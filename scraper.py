"""Async scraper for ricardo.ch listings.

Approach: probe random 10-digit listing IDs on https://www.ricardo.ch/de/a/{ID}/.
A listing is accepted only if the page contains a SOFORT KAUFEN button.
Extracted fields: title, date, price (Sofort-Kaufpreis), seller nickname.
Seller details are fetched from https://www.ricardo.ch/de/shop/{nick}/ratings/.
"""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Any

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─── HTTP headers ────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
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

# ─── Random-probe settings ────────────────────────────────────────────────────
# IDs like 1313109388 are 10 digits in the ~1.2B–1.5B range.
LISTING_ID_MIN = 1_200_000_000
LISTING_ID_MAX = 1_500_000_000
LISTING_PROBE_BATCH = 50         # IDs per probe round
LISTING_PROBE_CONCURRENCY = 10   # parallel fetches

# German month names / abbreviations used on Ricardo.ch
_DE_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "mar": 3,
    "apr": 4, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}


# ─── Data model ───────────────────────────────────────────────────────────────

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
            from_dt = _ensure_tz(datetime.fromisoformat(listing_from))
            if _ensure_tz(self.posted_at) < from_dt:
                return False

        if listing_to and self.posted_at:
            to_dt = _ensure_tz(datetime.fromisoformat(listing_to))
            if _ensure_tz(self.posted_at) > to_dt:
                return False

        if seller_reg_before and self.seller_registered:
            max_dt = _ensure_tz(datetime.fromisoformat(seller_reg_before))
            if _ensure_tz(self.seller_registered) > max_dt:
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
            f"💰 Цена (Sofort-Kaufpreis): {price_str}",
            f"🔗 <a href=\"{self.url}\">Ссылка на объявление</a>",
            f"📅 Дата публикации: {posted_str}",
            f"👤 Продавец: <b>{self.seller_name or 'Неизвестно'}</b>",
            f"📆 Mitglied seit: {reg_str}",
        ]
        if self.sold_count is not None:
            lines.append(f"📦 Продано: {self.sold_count}")
        if self.purchases_count is not None:
            lines.append(f"🛒 Покупок: {self.purchases_count}")
        if self.seller_url:
            lines.append(f"🏪 <a href=\"{self.seller_url}\">Профиль продавца</a>")
        return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ensure_tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_price(text: str) -> Optional[float]:
    """Extract a positive float from a price string like 'CHF 25.–' or '1'290.00'."""
    text = (
        text.strip()
        .replace("\xa0", "")
        .replace("\u2019", "")
        .replace("'", "")
        .replace("CHF", "")
        .replace("Fr.", "")
    )
    # Remove trailing em-dash (used for .00 in Swiss prices)
    text = re.sub(r"[–—.-]+$", "", text.strip())
    match = re.search(r"(\d[\d., ]*\d|\d)", text)
    if not match:
        return None
    raw = match.group(0).replace(" ", "").replace(",", ".")
    raw = raw.rstrip(".")
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


def _parse_german_datetime(text: str) -> Optional[datetime]:
    """Parse dates like '8. Apr. 2026, 17:45 Uhr' → datetime."""
    text = text.strip()
    # With time
    m = re.search(
        r"(\d{1,2})\.\s*(\w+\.?)\s*(\d{4})[,\s]+(\d{1,2}):(\d{2})",
        text, re.I,
    )
    if m:
        day = int(m.group(1))
        month_str = m.group(2).lower().rstrip(".")
        year = int(m.group(3))
        hour = int(m.group(4))
        minute = int(m.group(5))
        month = _DE_MONTHS.get(month_str[:3])
        if month:
            try:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            except ValueError:
                pass
    # Date only
    m = re.search(r"(\d{1,2})\.\s*(\w+\.?)\s*(\d{4})", text, re.I)
    if m:
        day = int(m.group(1))
        month_str = m.group(2).lower().rstrip(".")
        year = int(m.group(3))
        month = _DE_MONTHS.get(month_str[:3])
        if month:
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _try_parse_date(raw: str) -> Optional[datetime]:
    """Try ISO-8601 first, then German date format."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    return _parse_german_datetime(raw)


# ─── __NEXT_DATA__ helpers ────────────────────────────────────────────────────

def _extract_next_data(html: str) -> Optional[dict]:
    """Extract and parse the __NEXT_DATA__ JSON from a Next.js page."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _deep_get(d: Any, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return None
        if d is None:
            return None
    return d


def _find_article(nd: dict) -> Optional[dict]:
    """
    Locate the article/listing object inside __NEXT_DATA__.
    Tries several known Ricardo.ch key paths.
    """
    pp = _deep_get(nd, "props", "pageProps") or {}

    # Direct keys on pageProps
    for key in ("article", "listing", "item", "product", "data", "articleData"):
        c = pp.get(key)
        if isinstance(c, dict) and c.get("id"):
            return c

    # Nested under initialData / dehydratedState / serverData
    for wrapper in ("initialData", "dehydratedState", "serverData", "initialState"):
        w = pp.get(wrapper)
        if isinstance(w, dict):
            for inner in ("article", "listing", "item"):
                c = w.get(inner)
                if isinstance(c, dict) and c.get("id"):
                    return c
            # dehydratedState.queries[*].state.data
            for q in (w.get("queries") or []):
                state = _deep_get(q, "state", "data")
                if isinstance(state, dict) and state.get("id"):
                    return state

    return None


def _extract_price_from_article(article: dict) -> Optional[float]:
    """Extract buy-now price from an article dict."""
    # Direct numeric fields
    for key in (
        "buyNowPrice", "sofortKaufPreis", "sofortpreis", "fixedPrice",
        "currentPrice", "price", "buyItNowPrice",
    ):
        raw = article.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
        if isinstance(raw, dict):
            for ak in ("amount", "value", "chf", "price", "centAmount"):
                v = raw.get(ak)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v) / 100 if ak == "centAmount" else float(v)
        if isinstance(raw, str) and raw.strip():
            p = _parse_price(raw)
            if p:
                return p

    # Ricardo sometimes stores prices in a nested "prices" list/object
    prices = article.get("prices")
    if isinstance(prices, list):
        for p_obj in prices:
            if isinstance(p_obj, dict):
                t = (p_obj.get("type") or "").lower()
                if "buy" in t or "sofort" in t or "fixed" in t or "now" in t:
                    for ak in ("amount", "value", "price"):
                        v = p_obj.get(ak)
                        if isinstance(v, (int, float)) and v > 0:
                            return float(v)
    if isinstance(prices, dict):
        for k, v in prices.items():
            if isinstance(v, (int, float)) and v > 0:
                return float(v)

    return None


def _is_sofort_kaufen_article(article: dict) -> bool:
    """Return True if the article dict signals a buy-now listing."""
    # Explicit type/mode fields
    for key in ("type", "listingType", "articleType", "saleType", "sellingType"):
        val = str(article.get(key) or "").lower()
        if val and any(k in val for k in ("buy_now", "buynow", "sofort", "fixed")):
            return True
    # A buy-now price existing is sufficient signal
    return _extract_price_from_article(article) is not None


def _listing_from_next_data(nd: dict, listing_id: str, url: str) -> Optional["Listing"]:
    """Build a Listing from __NEXT_DATA__ JSON on an individual listing page."""
    article = _find_article(nd)
    if not article:
        return None

    if not _is_sofort_kaufen_article(article):
        return None

    price = _extract_price_from_article(article)

    # Title
    title: Optional[str] = None
    for key in ("title", "name", "articleTitle", "subject", "itemTitle"):
        t = article.get(key)
        if isinstance(t, str) and t.strip():
            title = t.strip()
            break
    if not title:
        return None

    # Date
    posted_at: Optional[datetime] = None
    for key in ("endDate", "startDate", "createdAt", "publishedAt",
                "insertionDate", "activationDate", "expiryDate"):
        raw = article.get(key)
        if raw:
            posted_at = _try_parse_date(str(raw))
            if posted_at:
                break

    # Seller
    seller_name = ""
    seller_url = ""
    for sk in ("seller", "vendor", "user", "article_seller"):
        s = article.get(sk)
        if isinstance(s, dict):
            for nk in ("nickname", "username", "name", "login", "shopName", "userId"):
                n = s.get(nk)
                if isinstance(n, str) and n.strip():
                    seller_name = n.strip()
                    break
        if seller_name:
            break
    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

    # Image
    image_url = ""
    for ik in ("images", "photos", "gallery"):
        imgs = article.get(ik)
        if isinstance(imgs, list) and imgs:
            first = imgs[0]
            if isinstance(first, str) and first.startswith("http"):
                image_url = first
            elif isinstance(first, dict):
                for uk in ("url", "src", "original", "large", "medium"):
                    v = first.get(uk)
                    if isinstance(v, str) and v.startswith("http"):
                        image_url = v
                        break
            break
    if not image_url:
        for ik in ("imageUrl", "thumbnailUrl", "image", "photo"):
            v = article.get(ik)
            if isinstance(v, str) and v.startswith("http"):
                image_url = v
                break

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


# ─── HTML fallback for individual listing pages ───────────────────────────────

def _listing_from_html(html: str, listing_id: str, url: str) -> Optional[Listing]:
    """
    Parse an individual listing page with BeautifulSoup.
    Requires the text "SOFORT KAUFEN" to be present anywhere on the page.
    Extracts: title (h1), date ("8. Apr. 2026, 17:45 Uhr"),
              price (Sofort-Kaufpreis label), seller (Verkäufer section).
    """
    # ── SOFORT KAUFEN check ────────────────────────────────────────────────
    # Check both raw HTML (catches JSON strings) and rendered text
    has_sofort = bool(re.search(r"sofort.{0,2}kauf", html, re.I))
    if not has_sofort:
        return None

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)

    # Double-check in rendered text (some pages embed it only in JSON)
    if not re.search(r"sofort.{0,2}kauf", page_text, re.I):
        # Still accept if it was in raw HTML (JSON data)
        pass

    # ── Title ─────────────────────────────────────────────────────────────
    title: Optional[str] = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        for cand in [
            soup.find(attrs={"data-testid": re.compile(r"title|name|heading", re.I)}),
            soup.find(class_=re.compile(r"title|heading|article.name|product.name", re.I)),
        ]:
            if cand:
                t = cand.get_text(strip=True)
                if t and len(t) > 3:
                    title = t
                    break
    if not title:
        return None

    # ── Date: "8. Apr. 2026, 17:45 Uhr" ──────────────────────────────────
    posted_at: Optional[datetime] = None
    # Look in all text nodes
    for node in soup.find_all(string=re.compile(r"\d{1,2}\.\s+\w+\.?\s+\d{4}", re.I)):
        dt = _parse_german_datetime(str(node))
        if dt:
            posted_at = dt
            break
    if not posted_at:
        # Try <time datetime="..."> elements
        for t_tag in soup.find_all("time"):
            da = t_tag.get("datetime") or t_tag.get_text(strip=True)
            if da:
                dt = _try_parse_date(da)
                if dt:
                    posted_at = dt
                    break

    # ── Price: find "Sofort-Kaufpreis" label, then read sibling/parent ────
    price: Optional[float] = None
    for label_text in (
        "Sofort-Kaufpreis", "sofortkaufpreis", "Sofort Kaufpreis",
        "Sofort-Kauf", "Buy Now Price",
    ):
        label = soup.find(string=re.compile(re.escape(label_text), re.I))
        if label:
            container = label.find_parent()
            if container:
                # Search siblings and parent
                search_nodes = list(container.next_siblings) + [container.parent]
                for node in search_nodes:
                    if not (node and hasattr(node, "get_text")):
                        continue
                    p = _parse_price(node.get_text(" ", strip=True))
                    if p:
                        price = p
                        break
            if price:
                break

    if price is None:
        # Generic: find any element with class containing "price" near buy-now context
        for tag in soup.find_all(class_=re.compile(r"price|preis|kaufpreis", re.I)):
            candidate = tag.get_text(" ", strip=True)
            # Skip "Startpreis" (auction starting price)
            if re.search(r"start|auction|gebot|bieten", candidate, re.I):
                continue
            p = _parse_price(candidate)
            if p:
                price = p
                break

    # ── Seller: find "Verkäufer" label, then nearby link ─────────────────
    seller_name = ""
    seller_url = ""
    vk_node = soup.find(string=re.compile(r"Verk[äa]ufer", re.I))
    if vk_node:
        container = vk_node.find_parent()
        # Walk up at most 3 levels to find a shop link
        for _ in range(3):
            if container is None:
                break
            lnk = container.find("a", href=re.compile(r"/de/shop/"))
            if lnk:
                m = re.search(r"/de/shop/([^/]+)/", lnk.get("href", ""))
                if m:
                    seller_name = m.group(1)
                    break
            container = container.parent

    if not seller_name:
        # Fallback: any /de/shop/ link on the page
        for lnk in soup.find_all("a", href=re.compile(r"/de/shop/")):
            m = re.search(r"/de/shop/([^/]+)/", lnk.get("href", ""))
            if m:
                seller_name = m.group(1)
                break

    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

    # ── Image ─────────────────────────────────────────────────────────────
    image_url = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http") and re.search(r"ricardo|cdn|media|img", src, re.I):
            image_url = src
            break

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
    session: aiohttp.ClientSession,
    seller_url: str,
) -> tuple[Optional[datetime], Optional[int], Optional[int]]:
    """
    Fetch https://www.ricardo.ch/de/shop/{nick}/ratings/
    and return (registration_date, sold_count, purchases_count).
    Looks for "Mitglied seit YYYY" pattern.
    """
    if not seller_url:
        return None, None, None
    ratings_url = (
        seller_url if "/ratings" in seller_url
        else seller_url.rstrip("/") + "/ratings/"
    )
    try:
        async with session.get(
            ratings_url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.debug("Seller page %s → HTTP %d", ratings_url, resp.status)
                return None, None, None
            html = await resp.text()
    except Exception as exc:
        logger.debug("Seller profile fetch error (%s): %s", ratings_url, exc)
        return None, None, None

    reg_date: Optional[datetime] = None
    sold_count: Optional[int] = None
    purchases_count: Optional[int] = None

    # ── 1st try: __NEXT_DATA__ JSON ───────────────────────────────────────
    nd = _extract_next_data(html)
    if nd:
        pp = _deep_get(nd, "props", "pageProps") or {}
        for pk in ("profile", "seller", "user", "shop", "shopUser", "shopProfile"):
            profile = pp.get(pk)
            if not isinstance(profile, dict):
                continue
            for rk in ("registrationDate", "memberSince", "createdAt",
                       "joinDate", "registration_date", "member_since"):
                raw = profile.get(rk)
                if raw:
                    reg_date = _try_parse_date(str(raw))
                    if not reg_date:
                        m = re.search(r"\b(20\d{2}|19\d{2})\b", str(raw))
                        if m:
                            reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
                    if reg_date:
                        break
            for sk in ("soldCount", "salesCount", "numberOfSales", "sellerRatingCount"):
                v = profile.get(sk)
                if isinstance(v, int):
                    sold_count = v
                    break
            for bk in ("purchaseCount", "buyerRatingCount", "numberOfPurchases"):
                v = profile.get(bk)
                if isinstance(v, int):
                    purchases_count = v
                    break
            if reg_date:
                break

    # ── 2nd try: HTML ─────────────────────────────────────────────────────
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)

    if not reg_date:
        # "Mitglied seit 2018" or "Mitglied seit 01.01.2018"
        m = re.search(r"Mitglied\s+seit\s+(\d{2}\.\d{2}\.(\d{4})|\d{4})", page_text, re.I)
        if m:
            full = m.group(1)
            if "." in full:
                # DD.MM.YYYY
                parts = full.split(".")
                try:
                    reg_date = datetime(int(parts[2]), int(parts[1]), int(parts[0]),
                                        tzinfo=timezone.utc)
                except (ValueError, IndexError):
                    pass
            else:
                reg_date = datetime(int(full), 1, 1, tzinfo=timezone.utc)

    if not reg_date:
        # Broader fallback: any year after "seit"
        m = re.search(r"seit\s+(20\d{2}|19\d{2})\b", page_text, re.I)
        if m:
            reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)

    if sold_count is None:
        # Ratings as seller
        m = re.search(
            r"als?\s+Verk[äa]ufer[^\d]*(\d[\d'.\s]*)",
            page_text, re.I,
        )
        if m:
            try:
                sold_count = int(re.sub(r"['\s.]", "", m.group(1)))
            except ValueError:
                pass

    if purchases_count is None:
        m = re.search(
            r"als?\s+K[äa]ufer[^\d]*(\d[\d'.\s]*)",
            page_text, re.I,
        )
        if m:
            try:
                purchases_count = int(re.sub(r"['\s.]", "", m.group(1)))
            except ValueError:
                pass

    return reg_date, sold_count, purchases_count


# ─── Individual listing page ──────────────────────────────────────────────────

async def fetch_listing_detail(
    session: aiohttp.ClientSession,
    listing_id: str,
) -> Optional[Listing]:
    """
    Fetch https://www.ricardo.ch/de/a/{listing_id}/ and return a Listing
    if and only if the page has a SOFORT KAUFEN offer.
    """
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
            final_url = str(resp.url)
            # Redirected away from /a/ = dead listing
            if "/a/" not in final_url and "ricardo" in final_url:
                return None
            html = await resp.text()
    except Exception as exc:
        logger.debug("fetch_listing_detail(%s) network error: %s", listing_id, exc)
        return None

    # 1st: parse __NEXT_DATA__
    nd = _extract_next_data(html)
    if nd:
        listing = _listing_from_next_data(nd, listing_id, url)
        if listing:
            logger.debug("✓ %s via JSON: %s", listing_id, listing.title)
            return listing

    # 2nd: HTML fallback
    listing = _listing_from_html(html, listing_id, url)
    if listing:
        logger.debug("✓ %s via HTML: %s", listing_id, listing.title)
    return listing


# ─── Seller enrichment ────────────────────────────────────────────────────────

async def enrich_seller_info(
    session: aiohttp.ClientSession,
    listings: list[Listing],
) -> None:
    tasks = [_enrich_one(session, lst) for lst in listings if lst.seller_url]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _enrich_one(session: aiohttp.ClientSession, listing: Listing) -> None:
    reg, sold, purchases = await fetch_seller_info(session, listing.seller_url)
    listing.seller_registered = reg
    listing.sold_count = sold
    listing.purchases_count = purchases


# ─── Main probe batch ─────────────────────────────────────────────────────────

async def probe_batch(
    session: aiohttp.ClientSession,
    n: int = LISTING_PROBE_BATCH,
) -> list[Listing]:
    """
    Probe *n* random listing IDs and return those that have SOFORT KAUFEN.
    IDs are 10-digit integers in the LISTING_ID_MIN..LISTING_ID_MAX range.
    """
    ids = [str(random.randint(LISTING_ID_MIN, LISTING_ID_MAX)) for _ in range(n)]
    sem = asyncio.Semaphore(LISTING_PROBE_CONCURRENCY)
    results: list[Listing] = []

    logger.info("🔍 Проверяем %d случайных ID [%d–%d]", n, LISTING_ID_MIN, LISTING_ID_MAX)

    async def probe_one(lid: str) -> None:
        async with sem:
            listing = await fetch_listing_detail(session, lid)
            if listing:
                logger.info("✅ Найдено объявление: [%s] %s (CHF %.0f)",
                            lid, listing.title, listing.price or 0)
                results.append(listing)
            await asyncio.sleep(0.3)

    await asyncio.gather(*[probe_one(lid) for lid in ids], return_exceptions=True)
    logger.info("📊 Батч завершён: %d/%d ID — найдено SOFORT KAUFEN объявлений", len(results), n)
    return results


# ─── Legacy API compat ────────────────────────────────────────────────────────

async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Legacy wrapper – kept for backward compatibility."""
    return await probe_batch(session)
