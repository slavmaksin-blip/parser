"""Async scraper for ricardo.ch listings."""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

# ─── Category definitions (kept for filter UI labels) ─────────────────────────
CATEGORIES: dict[str, str] = {
    "hats":              "Шляпы, шапки, кепки (женские)",
    "shoes_men":         "Мужская обувь",
    "wedding":           "Свадьба и аксессуары",
    "backpacks":         "Рюкзаки",
    "folk":              "Народная мода",
    "clothing":          "Одежда и аксессуары",
    "blouses":           "Блузки и туники",
    "accessories_women": "Аксессуары для женщин",
}

# ─── Random-probe constants ───────────────────────────────────────────────────
# Example IDs: 1313109388, 1307375512 – ID space ~1.2B–1.4B
LISTING_ID_MIN = 1_200_000_000
LISTING_ID_MAX = 1_400_000_000
LISTING_PROBE_BATCH  = 20   # IDs fetched in one probe batch
LISTING_PROBE_CONCURRENCY = 8

# German month abbreviations used on Ricardo.ch ("8. Apr. 2026, 17:45 Uhr")
_DE_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "mar": 3,
    "apr": 4, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}


@dataclass
class Listing:
    listing_id: str
    title: str
    price: Optional[float]
    url: str
    image_url: str = ""
    category: str = ""
    posted_at: Optional[datetime] = None
    seller_name: str = ""
    seller_url: str = ""
    seller_registered: Optional[datetime] = None
    sold_count: Optional[int] = None
    purchases_count: Optional[int] = None
    description: str = ""

    def matches(self, filters: dict) -> bool:
        """Return True if this listing passes all active filters."""
        min_p = filters.get("min_price")
        max_p = filters.get("max_price")
        seller_reg_before = filters.get("max_seller_reg_date")
        listing_from = filters.get("listing_date_from")
        listing_to = filters.get("listing_date_to")
        min_sold = filters.get("min_sold")
        min_purchases = filters.get("min_purchases")

        if min_p is not None and self.price is not None and self.price < min_p:
            return False
        if max_p is not None and self.price is not None and self.price > max_p:
            return False

        if listing_from and self.posted_at:
            from_dt = datetime.fromisoformat(listing_from)
            posted = self.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if from_dt.tzinfo is None:
                from_dt = from_dt.replace(tzinfo=timezone.utc)
            if posted < from_dt:
                return False

        if listing_to and self.posted_at:
            to_dt = datetime.fromisoformat(listing_to)
            posted = self.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if to_dt.tzinfo is None:
                to_dt = to_dt.replace(tzinfo=timezone.utc)
            if posted > to_dt:
                return False

        if seller_reg_before and self.seller_registered:
            max_dt = datetime.fromisoformat(seller_reg_before)
            reg = self.seller_registered
            if reg.tzinfo is None:
                reg = reg.replace(tzinfo=timezone.utc)
            if max_dt.tzinfo is None:
                max_dt = max_dt.replace(tzinfo=timezone.utc)
            if reg > max_dt:
                return False

        if min_sold is not None and self.sold_count is not None and self.sold_count < min_sold:
            return False
        if min_purchases is not None and self.purchases_count is not None and self.purchases_count < min_purchases:
            return False

        return True

    def format_message(self) -> str:
        price_str = f"CHF {self.price:.2f}" if self.price is not None else "Цена по запросу"
        posted_str = (
            self.posted_at.strftime("%d.%m.%Y %H:%M")
            if self.posted_at
            else "Неизвестно"
        )
        reg_str = (
            self.seller_registered.strftime("%Y")
            if self.seller_registered
            else "Неизвестно"
        )
        lines = [
            f"🛍 <b>{self.title}</b>",
            f"💰 Цена: {price_str}",
            f"🔗 <a href=\"{self.url}\">Ссылка на объявление</a>",
            f"📅 Дата публикации: {posted_str}",
            f"👤 Продавец: <b>{self.seller_name or 'Неизвестно'}</b>",
            f"📆 Дата регистрации продавца: {reg_str}",
        ]
        if self.sold_count is not None:
            lines.append(f"📦 Продано товаров: {self.sold_count}")
        if self.purchases_count is not None:
            lines.append(f"🛒 Покупок у продавца: {self.purchases_count}")
        if self.seller_url:
            lines.append(f"🏪 <a href=\"{self.seller_url}\">Профиль продавца</a>")
        return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> Optional[float]:
    text = text.strip().replace("\xa0", " ").replace("\u2019", "")
    match = re.search(r"[\d'., ]+", text)
    if not match:
        return None
    raw = match.group(0).replace("'", "").replace(" ", "").replace(",", ".")
    # Remove trailing dot
    raw = raw.rstrip(".")
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


def _parse_german_datetime(text: str) -> Optional[datetime]:
    """Parse Ricardo.ch listing date format: '8. Apr. 2026, 17:45 Uhr'."""
    text = text.strip()
    m = re.search(
        r"(\d{1,2})\.\s*(\w+\.?)\s*(\d{4})[,\s]+(\d{1,2}):(\d{2})",
        text, re.I,
    )
    if m:
        day, month_str, year, hour, minute = (
            int(m.group(1)), m.group(2).lower().rstrip("."),
            int(m.group(3)), int(m.group(4)), int(m.group(5)),
        )
        month = _DE_MONTHS.get(month_str[:3])
        if month:
            try:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            except ValueError:
                pass
    m = re.search(r"(\d{1,2})\.\s*(\w+\.?)\s*(\d{4})", text, re.I)
    if m:
        day, month_str, year = (
            int(m.group(1)), m.group(2).lower().rstrip("."), int(m.group(3))
        )
        month = _DE_MONTHS.get(month_str[:3])
        if month:
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _deep_get(d: Any, *keys: str) -> Any:
    """Safely navigate a nested dict/list."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int):
            d = d[k] if k < len(d) else None
        else:
            return None
        if d is None:
            return None
    return d


# ─── Next.js data extraction ──────────────────────────────────────────────────

def _extract_next_data(html: str) -> Optional[dict]:
    """Extract and parse the __NEXT_DATA__ JSON embedded in a Next.js page."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _listing_from_next_data(
    data: dict, listing_id: str, url: str
) -> Optional[Listing]:
    """
    Try to build a Listing from __NEXT_DATA__ JSON.
    Ricardo.ch stores article data under various key paths depending on version.
    """
    pp = _deep_get(data, "props", "pageProps") or {}

    # Find the article object – try several known field names
    article: Optional[dict] = None
    for key in ("article", "listing", "item", "product", "data"):
        candidate = pp.get(key)
        if isinstance(candidate, dict) and candidate:
            article = candidate
            break

    # Sometimes nested deeper
    if article is None:
        for key in ("initialData", "dehydratedState"):
            candidate = pp.get(key)
            if isinstance(candidate, dict):
                for inner_key in ("article", "listing", "item"):
                    a = candidate.get(inner_key)
                    if isinstance(a, dict) and a:
                        article = a
                        break
            if article:
                break

    if not article:
        return None

    # ── SOFORT KAUFEN: article must have a buy-now price ──────────────────
    buy_now_price: Optional[float] = None
    for pkey in ("buyNowPrice", "sofortKaufPreis", "buyNow", "fixedPrice", "sofortpreis"):
        raw = article.get(pkey)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            buy_now_price = float(raw)
            break
        if isinstance(raw, dict):
            for akey in ("amount", "value", "chf", "price"):
                v = raw.get(akey)
                if isinstance(v, (int, float)) and v > 0:
                    buy_now_price = float(v)
                    break
            if buy_now_price:
                break

    if not buy_now_price:
        return None

    # ── Title ─────────────────────────────────────────────────────────────
    title: Optional[str] = None
    for tkey in ("title", "name", "articleTitle", "itemTitle"):
        t = article.get(tkey)
        if isinstance(t, str) and t.strip():
            title = t.strip()
            break
    if not title:
        return None

    # ── Publication / end date ────────────────────────────────────────────
    posted_at: Optional[datetime] = None
    for dkey in ("endDate", "startDate", "createdAt", "publishedAt", "insertionDate"):
        raw_date = article.get(dkey)
        if not raw_date:
            continue
        if isinstance(raw_date, str):
            try:
                posted_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                break
            except ValueError:
                dt = _parse_german_datetime(raw_date)
                if dt:
                    posted_at = dt
                    break

    # ── Seller ────────────────────────────────────────────────────────────
    seller_name = ""
    seller_url = ""
    seller_obj = article.get("seller") or article.get("vendor") or article.get("user")
    if isinstance(seller_obj, dict):
        for nkey in ("nickname", "username", "name", "login", "userId"):
            n = seller_obj.get(nkey)
            if isinstance(n, str) and n.strip():
                seller_name = n.strip()
                break
    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

    # ── Image ─────────────────────────────────────────────────────────────
    image_url = ""
    imgs = article.get("images") or article.get("photos") or []
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            image_url = first
        elif isinstance(first, dict):
            image_url = (
                first.get("url") or first.get("src") or
                first.get("original") or first.get("large") or ""
            )
    if not image_url:
        img_raw = article.get("imageUrl") or article.get("thumbnailUrl") or ""
        if isinstance(img_raw, str):
            image_url = img_raw

    return Listing(
        listing_id=listing_id,
        title=title,
        price=buy_now_price,
        url=url,
        image_url=image_url,
        posted_at=posted_at,
        seller_name=seller_name,
        seller_url=seller_url,
    )


# ─── HTML fallback extraction ─────────────────────────────────────────────────

def _listing_from_html(
    html: str, listing_id: str, url: str
) -> Optional[Listing]:
    """Fallback: extract listing data from rendered HTML."""
    soup = BeautifulSoup(html, "lxml")

    # SOFORT KAUFEN must be present somewhere (text or JSON embedded)
    page_text = soup.get_text(" ", strip=True)
    has_sofort = bool(re.search(r"sofort\s*kaufen", page_text, re.I))
    if not has_sofort:
        # Also check raw HTML (may be in JSON strings)
        has_sofort = bool(re.search(r"sofort.?kaufen", html, re.I))
    if not has_sofort:
        return None

    # ── Title ─────────────────────────────────────────────────────────────
    title: Optional[str] = None
    for cand in [
        soup.find("h1"),
        soup.find(attrs={"data-testid": re.compile(r"title|name", re.I)}),
        soup.find(class_=re.compile(r"title|heading|product.name", re.I)),
    ]:
        if cand:
            t = cand.get_text(strip=True)
            if t:
                title = t
                break
    if not title:
        return None

    # ── Date ──────────────────────────────────────────────────────────────
    posted_at: Optional[datetime] = None
    for node in soup.find_all(string=re.compile(r"\d{1,2}\.\s+\w+\s+\d{4}", re.I)):
        dt = _parse_german_datetime(str(node))
        if dt:
            posted_at = dt
            break
    if not posted_at:
        for t in soup.find_all("time"):
            da = t.get("datetime", "")
            if da:
                try:
                    posted_at = datetime.fromisoformat(da.replace("Z", "+00:00"))
                    break
                except ValueError:
                    pass

    # ── Price ─────────────────────────────────────────────────────────────
    price: Optional[float] = None
    label = soup.find(string=re.compile(r"Sofort.Kaufpreis|sofortkaufpreis", re.I))
    if label:
        container = label.find_parent()
        if container:
            for sib in list(container.next_siblings) + [container.parent]:
                if sib and hasattr(sib, "get_text"):
                    p = _parse_price(sib.get_text(" ", strip=True))
                    if p:
                        price = p
                        break
    if price is None:
        for tag in soup.find_all(class_=re.compile(r"price|preis", re.I)):
            p = _parse_price(tag.get_text())
            if p:
                price = p
                break

    # ── Seller ────────────────────────────────────────────────────────────
    seller_name = ""
    seller_url = ""
    vk = soup.find(string=re.compile(r"Verk[äa]ufer", re.I))
    if vk:
        root = vk.find_parent()
        if root:
            root = root.parent or root
        if root:
            lnk = root.find("a", href=re.compile(r"/de/shop/"))
            if lnk:
                m = re.search(r"/de/shop/([^/]+)/", lnk.get("href", ""))
                if m:
                    seller_name = m.group(1)
    if not seller_name:
        for lnk in soup.find_all("a", href=re.compile(r"/de/shop/")):
            m = re.search(r"/de/shop/([^/]+)/", lnk.get("href", ""))
            if m:
                seller_name = m.group(1)
                break
    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

    # ── Image ─────────────────────────────────────────────────────────────
    image_url = ""
    img = soup.find("img", src=re.compile(r"ricardo|cdn|img", re.I))
    if not img:
        img = soup.find("img")
    if img:
        image_url = img.get("src") or img.get("data-src") or ""

    return Listing(
        listing_id=listing_id,
        title=title,
        price=price,
        url=url,
        image_url=image_url,
        posted_at=posted_at,
        seller_name=seller_name,
        seller_url=seller_url,
    )


# ─── Seller profile ───────────────────────────────────────────────────────────

async def fetch_seller_info(
    session: aiohttp.ClientSession, seller_url: str
) -> tuple[Optional[datetime], Optional[int], Optional[int]]:
    """Fetch seller ratings page and return (registration_date, sold_count, purchases_count)."""
    if not seller_url:
        return None, None, None
    ratings_url = seller_url if "/ratings" in seller_url else seller_url.rstrip("/") + "/ratings/"
    try:
        async with session.get(
            ratings_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=12)
        ) as resp:
            if resp.status != 200:
                return None, None, None
            html = await resp.text()

        # Try __NEXT_DATA__ first
        nd = _extract_next_data(html)
        if nd:
            pp = (_deep_get(nd, "props", "pageProps") or {})
            profile = pp.get("profile") or pp.get("seller") or pp.get("user") or pp.get("shop") or {}
            if isinstance(profile, dict):
                reg_date = None
                for rk in ("registrationDate", "memberSince", "createdAt", "joinDate"):
                    raw = profile.get(rk)
                    if raw:
                        try:
                            reg_date = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        except ValueError:
                            m = re.search(r"\b(20\d{2}|19\d{2})\b", str(raw))
                            if m:
                                reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
                        if reg_date:
                            break
                sold = None
                for sk in ("soldCount", "salesCount", "numberOfSales", "sellerRatingCount"):
                    v = profile.get(sk)
                    if isinstance(v, int):
                        sold = v
                        break
                purchases = None
                for bk in ("purchaseCount", "buyerRatingCount", "numberOfPurchases"):
                    v = profile.get(bk)
                    if isinstance(v, int):
                        purchases = v
                        break
                if reg_date:
                    return reg_date, sold, purchases

        soup = BeautifulSoup(html, "lxml")
        reg_date = None
        sold_count = None
        purchases_count = None

        # Registration date
        for tag in soup.find_all(string=re.compile(r"Mitglied seit|member since", re.I)):
            parent = tag.parent
            text = parent.get_text(" ", strip=True)
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if m:
                reg_date = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
                break
            m = re.search(r"\b(20\d{2}|19\d{2})\b", text)
            if m:
                reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
                break

        # Sold count
        for tag in soup.find_all(string=re.compile(r"Verk[äa]ufer|Verk[äa]ufe|as seller", re.I)):
            text = tag.parent.get_text(" ", strip=True)
            m = re.search(r"(\d[\d\s']*)", text)
            if m:
                try:
                    sold_count = int(m.group(1).replace(" ", "").replace("'", ""))
                    break
                except ValueError:
                    pass

        # Purchases count
        for tag in soup.find_all(string=re.compile(r"K[äa]ufer|K[äa]ufe|as buyer", re.I)):
            text = tag.parent.get_text(" ", strip=True)
            m = re.search(r"(\d[\d\s']*)", text)
            if m:
                try:
                    purchases_count = int(m.group(1).replace(" ", "").replace("'", ""))
                    break
                except ValueError:
                    pass

        return reg_date, sold_count, purchases_count
    except Exception as exc:
        logger.debug("seller profile fetch error: %s", exc)
    return None, None, None


# ─── Listing detail page ──────────────────────────────────────────────────────

async def fetch_listing_detail(
    session: aiohttp.ClientSession, listing_id: str
) -> Optional[Listing]:
    """Fetch a single listing page by ID and return a Listing if it has SOFORT KAUFEN."""
    url = f"https://www.ricardo.ch/de/a/{listing_id}/"
    try:
        async with session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None
            # Dead listing – redirected away from /a/ path
            final_url = str(resp.url)
            if "/a/" not in final_url and "ricardo" in final_url:
                return None
            html = await resp.text()
    except Exception as exc:
        logger.debug("fetch_listing_detail(%s) error: %s", listing_id, exc)
        return None

    # 1st try: parse __NEXT_DATA__ JSON (most reliable for Next.js sites)
    nd = _extract_next_data(html)
    if nd:
        listing = _listing_from_next_data(nd, listing_id, url)
        if listing:
            logger.debug("Got listing %s via __NEXT_DATA__: %s", listing_id, listing.title)
            return listing

    # 2nd try: HTML parsing fallback
    listing = _listing_from_html(html, listing_id, url)
    if listing:
        logger.debug("Got listing %s via HTML fallback: %s", listing_id, listing.title)
    return listing


# ─── Probe batch ──────────────────────────────────────────────────────────────

async def probe_batch(
    session: aiohttp.ClientSession,
    n: int = LISTING_PROBE_BATCH,
) -> list[Listing]:
    """Probe *n* random listing IDs and return those that have SOFORT KAUFEN."""
    ids = [str(random.randint(LISTING_ID_MIN, LISTING_ID_MAX)) for _ in range(n)]
    sem = asyncio.Semaphore(LISTING_PROBE_CONCURRENCY)
    results: list[Listing] = []

    async def probe_one(lid: str) -> None:
        async with sem:
            listing = await fetch_listing_detail(session, lid)
            if listing:
                results.append(listing)
            await asyncio.sleep(0.5)

    await asyncio.gather(*[probe_one(lid) for lid in ids], return_exceptions=True)
    return results


async def enrich_seller_info(
    session: aiohttp.ClientSession, listings: list[Listing]
) -> None:
    """Fetch seller registration dates and stats for all listings."""
    tasks = [_enrich_one(session, lst) for lst in listings if lst.seller_url]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _enrich_one(session: aiohttp.ClientSession, listing: Listing) -> None:
    reg, sold, purchases = await fetch_seller_info(session, listing.seller_url)
    listing.seller_registered = reg
    listing.sold_count = sold
    listing.purchases_count = purchases


# ─── Legacy API compat ────────────────────────────────────────────────────────

async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Legacy wrapper – probes a batch of random IDs."""
    return await probe_batch(session)
