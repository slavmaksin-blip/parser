"""Async scraper for ricardo.ch listings."""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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

# ─── Category pages: sorted newest-first ────────────────────────────────────
# Used as PRIMARY listing source – these pages always return real listings.
SEARCH_URLS: list[str] = [
    "https://www.ricardo.ch/de/s/?q=&sort=newest",
    "https://www.ricardo.ch/de/c/kleidung-accessoires-40842/?sort=newest",
    "https://www.ricardo.ch/de/c/elektronik-8691/?sort=newest",
    "https://www.ricardo.ch/de/c/ruecksaecke-63791/?sort=newest",
    "https://www.ricardo.ch/de/c/herrenschuhe-40822/?sort=newest",
    "https://www.ricardo.ch/de/c/accessoires-fuer-damen-40749/?sort=newest",
    "https://www.ricardo.ch/de/c/blusen-und-tunika-40780/?sort=newest",
    "https://www.ricardo.ch/de/c/huete-muetzen-caps-fuer-damen-73899/?sort=newest",
    "https://www.ricardo.ch/de/c/hochzeit-hochzeitsdeko-zubehoer-40836/?sort=newest",
    "https://www.ricardo.ch/de/c/trachtenmode-40839/?sort=newest",
]

# Concurrency limit for individual listing detail requests
LISTING_DETAIL_CONCURRENCY = 6

# German month abbreviations used on Ricardo.ch ("8. Apr. 2026, 17:45 Uhr")
_DE_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "mar": 3,
    "apr": 4, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}

# ─── Fields that identify a "listing-like" object in JSON ────────────────────
_LISTING_ID_FIELDS = {"id", "articleId", "listingId", "article_id", "listing_id"}
_LISTING_TITLE_FIELDS = {"title", "name", "articleTitle", "subject"}
_LISTING_PRICE_FIELDS = {
    "buyNowPrice", "price", "currentPrice", "sofortKaufpreis",
    "buyItNow", "sofortpreis", "actualPrice",
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
            self.seller_registered.strftime("%d.%m.%Y")
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
    text = text.strip().replace("\xa0", " ").replace("\u2019", "").replace("'", "")
    match = re.search(r"[\d., ]+", text)
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


def _try_parse_date(raw: str) -> Optional[datetime]:
    """Try ISO-8601 then German date formats."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    return _parse_german_datetime(raw)


# ─── Next.js __NEXT_DATA__ extraction ────────────────────────────────────────

def _extract_next_data(html: str) -> Optional[dict]:
    """Extract and parse the __NEXT_DATA__ JSON embedded in a Next.js page."""
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


def _find_listing_objects(node: Any, depth: int = 0) -> list[dict]:
    """
    Recursively walk a JSON structure and collect every dict that looks
    like a Ricardo.ch listing (has an id-like field AND a title-like field).
    """
    if depth > 8:
        return []
    found: list[dict] = []
    if isinstance(node, dict):
        has_id = bool(_LISTING_ID_FIELDS & node.keys())
        has_title = bool(_LISTING_TITLE_FIELDS & node.keys())
        if has_id and has_title:
            found.append(node)
        # Always recurse to find nested listings
        for v in node.values():
            found.extend(_find_listing_objects(v, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_listing_objects(item, depth + 1))
    return found


def _extract_str_id(obj: dict) -> Optional[str]:
    """Extract a listing ID string from a listing-like dict."""
    for k in ("id", "articleId", "listingId", "article_id", "listing_id"):
        v = obj.get(k)
        if v is not None:
            s = str(v).strip()
            if s and s != "0":
                return s
    return None


def _extract_price(obj: dict) -> Optional[float]:
    """Extract a numeric price from a listing-like dict."""
    for k in _LISTING_PRICE_FIELDS:
        raw = obj.get(k)
        if raw is None:
            continue
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
        if isinstance(raw, dict):
            for ak in ("amount", "value", "chf", "price", "centAmount"):
                v = raw.get(ak)
                if isinstance(v, (int, float)) and v > 0:
                    # centAmount is in cents
                    return float(v) / 100 if ak == "centAmount" else float(v)
        if isinstance(raw, str):
            p = _parse_price(raw)
            if p:
                return p
    return None


def _extract_seller(obj: dict) -> tuple[str, str]:
    """Return (seller_name, seller_url) from a listing dict."""
    for sk in ("seller", "vendor", "user", "article_seller"):
        s = obj.get(sk)
        if isinstance(s, dict):
            for nk in ("nickname", "username", "name", "login", "shopName"):
                n = s.get(nk)
                if isinstance(n, str) and n.strip():
                    name = n.strip()
                    return name, f"https://www.ricardo.ch/de/shop/{name}/ratings/"
    return "", ""


def _extract_image(obj: dict) -> str:
    """Extract a main image URL from a listing dict."""
    for ik in ("images", "photos", "gallery"):
        imgs = obj.get(ik)
        if isinstance(imgs, list) and imgs:
            first = imgs[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                for uk in ("url", "src", "original", "large", "medium", "small"):
                    v = first.get(uk)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
    for ik in ("imageUrl", "thumbnailUrl", "image", "photo"):
        v = obj.get(ik)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def _build_listing_url(listing_id: str) -> str:
    return f"https://www.ricardo.ch/de/a/{listing_id}/"


def _listing_from_obj(obj: dict) -> Optional[Listing]:
    """Build a Listing from a listing-like dict found in __NEXT_DATA__."""
    listing_id = _extract_str_id(obj)
    if not listing_id:
        return None

    title = None
    for k in ("title", "name", "articleTitle", "subject"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    if not title:
        return None

    price = _extract_price(obj)

    posted_at: Optional[datetime] = None
    for dk in ("endDate", "startDate", "createdAt", "publishedAt", "insertionDate",
               "activationDate", "expiryDate", "created_at"):
        raw = obj.get(dk)
        if raw:
            posted_at = _try_parse_date(str(raw))
            if posted_at:
                break

    seller_name, seller_url = _extract_seller(obj)
    image_url = _extract_image(obj)
    url = _build_listing_url(listing_id)

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


# ─── HTML fallback extraction (for individual listing pages) ─────────────────

def _listing_from_html(html: str, listing_id: str, url: str) -> Optional[Listing]:
    """HTML-only extraction from a single listing page."""
    soup = BeautifulSoup(html, "lxml")

    # Title
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

    # Date
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
                dt = _try_parse_date(da)
                if dt:
                    posted_at = dt
                    break

    # Price
    price: Optional[float] = None
    for tag in soup.find_all(class_=re.compile(r"price|preis", re.I)):
        p = _parse_price(tag.get_text())
        if p:
            price = p
            break

    # Seller
    seller_name = ""
    seller_url = ""
    for lnk in soup.find_all("a", href=re.compile(r"/de/shop/")):
        m = re.search(r"/de/shop/([^/]+)/", lnk.get("href", ""))
        if m:
            seller_name = m.group(1)
            seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"
            break

    # Image
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


# ─── Search / category page fetching ─────────────────────────────────────────

async def fetch_page_listings(
    session: aiohttp.ClientSession,
    page_url: str,
) -> list[Listing]:
    """
    Fetch a search/category page and return all listings found in it.
    Tries __NEXT_DATA__ JSON first, then HTML link extraction.
    """
    try:
        async with session.get(
            page_url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.warning("Страница %s → HTTP %d", page_url, resp.status)
                return []
            html = await resp.text()
            logger.info("Страница получена: %s (HTTP 200, %d байт)", page_url, len(html))
    except Exception as exc:
        logger.warning("Ошибка загрузки страницы %s: %s", page_url, exc)
        return []

    listings: list[Listing] = []

    # 1st try: __NEXT_DATA__ JSON
    nd = _extract_next_data(html)
    if nd:
        objs = _find_listing_objects(nd)
        logger.info("__NEXT_DATA__: найдено %d объектов-объявлений на %s", len(objs), page_url)
        for obj in objs:
            lst = _listing_from_obj(obj)
            if lst and lst.listing_id:
                listings.append(lst)
    else:
        logger.warning("__NEXT_DATA__ не найден на %s, использую HTML", page_url)

    # 2nd try: extract listing URLs from HTML links
    if not listings:
        soup = BeautifulSoup(html, "lxml")
        seen_ids: set[str] = set()
        for a in soup.find_all("a", href=re.compile(r"/de/a/\d+")):
            href = a.get("href", "")
            m = re.search(r"/de/a/(\d+)", href)
            if m:
                lid = m.group(1)
                if lid not in seen_ids:
                    seen_ids.add(lid)
                    listings.append(Listing(
                        listing_id=lid,
                        title=a.get_text(strip=True) or lid,
                        price=None,
                        url=_build_listing_url(lid),
                    ))
        logger.info("HTML ссылки: найдено %d объявлений на %s", len(listings), page_url)

    # Deduplicate by listing_id
    seen: set[str] = set()
    unique: list[Listing] = []
    for lst in listings:
        if lst.listing_id not in seen:
            seen.add(lst.listing_id)
            unique.append(lst)
    return unique


# ─── Individual listing detail enrichment ────────────────────────────────────

async def fetch_listing_detail(
    session: aiohttp.ClientSession,
    listing: Listing,
) -> Optional[Listing]:
    """
    Visit the individual listing page and fill in any missing fields.
    Returns the same listing (mutated) or None if the page is dead.
    """
    url = listing.url or _build_listing_url(listing.listing_id)
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
            # Redirected away from /a/ path = dead listing
            if "/a/" not in final_url and "ricardo" in final_url:
                return None
            html = await resp.text()
    except Exception as exc:
        logger.debug("fetch_listing_detail(%s) error: %s", listing.listing_id, exc)
        return None

    # Try __NEXT_DATA__ for full data
    nd = _extract_next_data(html)
    if nd:
        objs = _find_listing_objects(nd)
        for obj in objs:
            lid = _extract_str_id(obj)
            if lid == listing.listing_id:
                enriched = _listing_from_obj(obj)
                if enriched:
                    return enriched
        # Any object on the detail page is the listing
        if objs:
            enriched = _listing_from_obj(objs[0])
            if enriched:
                return enriched

    # HTML fallback
    html_listing = _listing_from_html(html, listing.listing_id, url)
    if html_listing:
        # Merge: HTML data fills gaps
        if not listing.title or listing.title == listing.listing_id:
            listing.title = html_listing.title
        if listing.price is None:
            listing.price = html_listing.price
        if not listing.seller_name:
            listing.seller_name = html_listing.seller_name
            listing.seller_url = html_listing.seller_url
        if listing.posted_at is None:
            listing.posted_at = html_listing.posted_at
        if not listing.image_url:
            listing.image_url = html_listing.image_url
        return listing

    # Page loaded but we couldn't parse it – return with whatever we have
    return listing if listing.title and listing.title != listing.listing_id else None


# ─── Seller profile ───────────────────────────────────────────────────────────

async def fetch_seller_info(
    session: aiohttp.ClientSession,
    seller_url: str,
) -> tuple[Optional[datetime], Optional[int], Optional[int]]:
    """Fetch seller ratings page and return (registration_date, sold_count, purchases_count)."""
    if not seller_url:
        return None, None, None
    ratings_url = (
        seller_url if "/ratings" in seller_url
        else seller_url.rstrip("/") + "/ratings/"
    )
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
            objs = _find_listing_objects(nd)
            # Look for a user/profile object in the JSON
            pp = (nd.get("props") or {}).get("pageProps") or {}
            for pk in ("profile", "seller", "user", "shop", "shopUser"):
                profile = pp.get(pk)
                if isinstance(profile, dict):
                    reg_date: Optional[datetime] = None
                    for rk in ("registrationDate", "memberSince", "createdAt", "joinDate",
                               "registration_date", "member_since"):
                        raw = profile.get(rk)
                        if raw:
                            reg_date = _try_parse_date(str(raw))
                            if not reg_date:
                                m = re.search(r"\b(20\d{2}|19\d{2})\b", str(raw))
                                if m:
                                    reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
                            if reg_date:
                                break
                    sold: Optional[int] = None
                    for sk in ("soldCount", "salesCount", "numberOfSales", "sellerRatingCount",
                               "sold_count", "sales_count"):
                        v = profile.get(sk)
                        if isinstance(v, int):
                            sold = v
                            break
                    purchases: Optional[int] = None
                    for bk in ("purchaseCount", "buyerRatingCount", "numberOfPurchases",
                               "purchase_count"):
                        v = profile.get(bk)
                        if isinstance(v, int):
                            purchases = v
                            break
                    if reg_date:
                        return reg_date, sold, purchases

        # HTML fallback
        soup = BeautifulSoup(html, "lxml")
        reg_date_h: Optional[datetime] = None
        sold_count: Optional[int] = None
        purchases_count: Optional[int] = None

        for tag in soup.find_all(string=re.compile(r"Mitglied seit|member since", re.I)):
            text = tag.parent.get_text(" ", strip=True)
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if m:
                reg_date_h = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                      tzinfo=timezone.utc)
                break
            m = re.search(r"\b(20\d{2}|19\d{2})\b", text)
            if m:
                reg_date_h = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
                break

        for tag in soup.find_all(string=re.compile(r"Verk[äa]ufer|Verk[äa]ufe|as seller", re.I)):
            text = tag.parent.get_text(" ", strip=True)
            mm = re.search(r"(\d[\d\s']*)", text)
            if mm:
                try:
                    sold_count = int(mm.group(1).replace(" ", "").replace("'", ""))
                    break
                except ValueError:
                    pass

        for tag in soup.find_all(string=re.compile(r"K[äa]ufer|K[äa]ufe|as buyer", re.I)):
            text = tag.parent.get_text(" ", strip=True)
            mm = re.search(r"(\d[\d\s']*)", text)
            if mm:
                try:
                    purchases_count = int(mm.group(1).replace(" ", "").replace("'", ""))
                    break
                except ValueError:
                    pass

        return reg_date_h, sold_count, purchases_count
    except Exception as exc:
        logger.debug("seller profile fetch error for %s: %s", seller_url, exc)
    return None, None, None


# ─── Enrichment ───────────────────────────────────────────────────────────────

async def enrich_seller_info(
    session: aiohttp.ClientSession,
    listings: list[Listing],
) -> None:
    """Fetch seller registration dates and stats for all listings with a seller URL."""
    tasks = [_enrich_one(session, lst) for lst in listings if lst.seller_url]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _enrich_one(session: aiohttp.ClientSession, listing: Listing) -> None:
    reg, sold, purchases = await fetch_seller_info(session, listing.seller_url)
    listing.seller_registered = reg
    listing.sold_count = sold
    listing.purchases_count = purchases


# ─── Main public API ──────────────────────────────────────────────────────────

async def probe_batch(
    session: aiohttp.ClientSession,
) -> list[Listing]:
    """
    Fetch one search/category page (rotating through SEARCH_URLS) and
    return all listings found on it with detail pages fetched.
    """
    # Rotate through search URLs randomly for variety
    page_url = random.choice(SEARCH_URLS)
    logger.info("▶ Сканируем страницу: %s", page_url)

    summaries = await fetch_page_listings(session, page_url)
    if not summaries:
        logger.info("Страница не вернула объявлений: %s", page_url)
        return []

    logger.info("Найдено %d объявлений на странице. Загружаем детали...", len(summaries))

    # Enrich summaries that are missing key data by visiting their detail pages
    sem = asyncio.Semaphore(LISTING_DETAIL_CONCURRENCY)
    results: list[Listing] = []

    async def enrich_one(lst: Listing) -> None:
        # If we already have title + price from the page JSON, skip the detail request
        if lst.title and lst.title != lst.listing_id and lst.price is not None and lst.seller_name:
            results.append(lst)
            return
        async with sem:
            detailed = await fetch_listing_detail(session, lst)
            if detailed:
                results.append(detailed)
            await asyncio.sleep(0.3)

    await asyncio.gather(*[enrich_one(s) for s in summaries], return_exceptions=True)

    logger.info("Итого пригодных объявлений: %d", len(results))
    return results


# ─── Legacy API compat ────────────────────────────────────────────────────────

async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Legacy wrapper – kept for backward compatibility."""
    return await probe_batch(session)
