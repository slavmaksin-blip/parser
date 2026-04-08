"""Async scraper for ricardo.ch listings.

Approach 1 (keyword search): fetch search results page for given keywords.
Approach 2 (probe fallback): probe random 10-digit listing IDs starting with "131".
"""

import asyncio
import json
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any

import aiohttp
from bs4 import BeautifulSoup
from loguru import logger

# ─── User-Agent pool ──────────────────────────────────────────────────────────
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

try:
    from fake_useragent import UserAgent as _UA
    _ua_gen = _UA()

    def _random_ua() -> str:
        try:
            return _ua_gen.random
        except Exception:
            return random.choice(_UA_POOL)
except Exception:
    def _random_ua() -> str:
        return random.choice(_UA_POOL)


def _headers() -> dict:
    return {
        "User-Agent": _random_ua(),
        "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


# ─── Category definitions ──────────────────────────────────────────────────────
CATEGORIES: dict[str, str] = {
    # Root categories
    "elektronik":  "Elektronik",
    "mode":        "Mode & Accessoires",
    "auto":        "Auto, Motorrad & Fahrrad",
    "haus":        "Haus & Garten",
    "sammeln":     "Sammeln & Seltenes",
    "sport":       "Sport & Freizeit",
    "uhren":       "Uhren & Schmuck",
    "baby":        "Baby & Kind",
    "buecher":     "Bücher, Filme & Musik",
    # Sub-categories
    "smartphones": "Smartphones",
    "laptops":     "Laptops & Computer",
    "tablets":     "Tablets",
    "tv":          "TV & Audio",
    "kameras":     "Kameras & Zubehör",
    "herren":      "Herrenbekleidung",
    "damen":       "Damenbekleidung",
    "schuhe":      "Schuhe",
    "taschen":     "Taschen & Geldbörsen",
    "fahrrad":     "Fahrräder",
    "moebel":      "Möbel",
    "garten":      "Garten",
}

# ─── Probe settings ────────────────────────────────────────────────────────────
LISTING_ID_PREFIX_MIN = 1_310_000_000
LISTING_ID_PREFIX_MAX = 1_319_999_999
LISTING_PROBE_BATCH = 100
LISTING_PROBE_CONCURRENCY = 10

# German month abbreviations
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
    listing_type: str = ""
    condition: str = ""
    location: str = ""
    delivery: str = ""
    views_count: Optional[int] = None
    bids_count: Optional[int] = None
    end_date: Optional[datetime] = None
    seller_rating: Optional[float] = None

    def matches(self, filters: dict) -> bool:
        """Return True if this listing passes all active filters."""
        min_p = filters.get("min_price")
        max_p = filters.get("max_price")
        seller_reg_before = filters.get("max_seller_reg_date")
        min_sold = filters.get("min_sold")
        f_listing_type = filters.get("listing_type")
        f_condition = filters.get("condition")
        f_location = filters.get("location")
        f_delivery = filters.get("delivery")
        keywords = filters.get("keywords") or []
        categories = filters.get("categories") or []

        if min_p is not None and self.price is not None and self.price < min_p:
            return False
        if max_p is not None and self.price is not None and self.price > max_p:
            return False

        if seller_reg_before and self.seller_registered:
            max_dt = _ensure_tz(datetime.fromisoformat(seller_reg_before))
            if _ensure_tz(self.seller_registered) > max_dt:
                return False

        if min_sold is not None and self.sold_count is not None and self.sold_count < min_sold:
            return False

        if f_listing_type and f_listing_type.lower() not in ("все", "all", ""):
            if self.listing_type and f_listing_type.lower() not in self.listing_type.lower():
                return False

        if f_condition and f_condition.lower() not in ("alle", "all", "все", ""):
            if self.condition and f_condition.lower() not in self.condition.lower():
                return False

        if f_location and f_location.strip():
            if self.location and f_location.lower() not in self.location.lower():
                return False

        if f_delivery and f_delivery.lower() not in ("beides", "all", "все", ""):
            if self.delivery and f_delivery.lower() not in self.delivery.lower():
                return False

        if categories:
            if self.category:
                cat_lower = self.category.lower()
                if not any(c.lower() in cat_lower or cat_lower in c.lower()
                           for c in categories):
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
            f"💰 {price_str}",
        ]
        if self.category:
            lines.append(f"📂 {self.category}")
        if self.location:
            lines.append(f"📍 {self.location}")
        if self.condition:
            lines.append(f"📦 Состояние: {self.condition}")
        if self.listing_type:
            lines.append(f"🏷 Тип: {self.listing_type}")
        lines.append(f"📅 Опубликовано: {posted_str}")
        if self.end_date:
            lines.append(f"⏰ Окончание: {self.end_date.strftime('%d.%m.%Y %H:%M')}")
        lines.append(f"👤 Продавец: <b>{self.seller_name or 'Неизвестно'}</b> (с {reg_str})")
        if self.sold_count is not None:
            lines.append(f"📦 Продано: {self.sold_count}")
        if self.seller_rating is not None:
            lines.append(f"⭐ Рейтинг: {self.seller_rating:.1f}")
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
    pp = _deep_get(nd, "props", "pageProps") or {}
    for key in ("article", "listing", "item", "product", "data", "articleData"):
        c = pp.get(key)
        if isinstance(c, dict) and c.get("id"):
            return c
    for wrapper in ("initialData", "dehydratedState", "serverData", "initialState"):
        w = pp.get(wrapper)
        if isinstance(w, dict):
            for inner in ("article", "listing", "item"):
                c = w.get(inner)
                if isinstance(c, dict) and c.get("id"):
                    return c
            for q in (w.get("queries") or []):
                state = _deep_get(q, "state", "data")
                if isinstance(state, dict) and state.get("id"):
                    return state
    return None


def _extract_price_from_article(article: dict) -> Optional[float]:
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
    for key in ("type", "listingType", "articleType", "saleType", "sellingType"):
        val = str(article.get(key) or "").lower()
        if val and any(k in val for k in ("buy_now", "buynow", "sofort", "fixed")):
            return True
    return _extract_price_from_article(article) is not None


def _listing_from_next_data(nd: dict, listing_id: str, url: str) -> Optional[Listing]:
    article = _find_article(nd)
    if not article:
        return None
    if not _is_sofort_kaufen_article(article):
        return None

    price = _extract_price_from_article(article)

    title: Optional[str] = None
    for key in ("title", "name", "articleTitle", "subject", "itemTitle"):
        t = article.get(key)
        if isinstance(t, str) and t.strip():
            title = t.strip()
            break
    if not title:
        return None

    posted_at: Optional[datetime] = None
    for key in ("endDate", "startDate", "createdAt", "publishedAt",
                "insertionDate", "activationDate", "expiryDate"):
        raw = article.get(key)
        if raw:
            posted_at = _try_parse_date(str(raw))
            if posted_at:
                break

    end_date: Optional[datetime] = None
    for key in ("endDate", "auctionEndDate", "expiryDate"):
        raw = article.get(key)
        if raw:
            end_date = _try_parse_date(str(raw))
            if end_date:
                break

    seller_name = ""
    seller_url = ""
    seller_rating: Optional[float] = None
    for sk in ("seller", "vendor", "user", "article_seller"):
        s = article.get(sk)
        if isinstance(s, dict):
            for nk in ("nickname", "username", "name", "login", "shopName", "userId"):
                n = s.get(nk)
                if isinstance(n, str) and n.strip():
                    seller_name = n.strip()
                    break
            for rk in ("rating", "sellerRating", "score"):
                rv = s.get(rk)
                if isinstance(rv, (int, float)):
                    seller_rating = float(rv)
                    break
        if seller_name:
            break
    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

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

    # listing_type
    listing_type = ""
    for key in ("type", "listingType", "articleType", "saleType", "sellingType"):
        raw = str(article.get(key) or "").lower()
        if "sofort" in raw or "buy_now" in raw or "buynow" in raw or "fixed" in raw:
            listing_type = "Sofortkauf"
            break
        elif "auction" in raw or "auktion" in raw or "bieten" in raw:
            listing_type = "Auktion"
            break
        elif "festpreis" in raw or "fixed_price" in raw:
            listing_type = "Festpreis"
            break

    # condition
    condition = ""
    for key in ("condition", "itemCondition", "articleCondition", "zustand"):
        raw = str(article.get(key) or "")
        if raw.strip():
            condition = raw.strip()
            break

    # location
    location = ""
    for key in ("location", "city", "zip", "address", "region"):
        raw = article.get(key)
        if isinstance(raw, str) and raw.strip():
            location = raw.strip()
            break
        elif isinstance(raw, dict):
            city = raw.get("city") or raw.get("zip") or ""
            if city:
                location = str(city)
                break

    # delivery
    delivery = ""
    for key in ("delivery", "deliveryOptions", "shipping", "versand"):
        raw = article.get(key)
        if isinstance(raw, str) and raw.strip():
            delivery = raw.strip()
            break

    # category
    category = ""
    for key in ("category", "categoryName", "mainCategory"):
        raw = article.get(key)
        if isinstance(raw, str) and raw.strip():
            category = raw.strip()
            break
        elif isinstance(raw, dict):
            for ck in ("name", "label", "title"):
                cv = raw.get(ck)
                if isinstance(cv, str) and cv.strip():
                    category = cv.strip()
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
        seller_rating=seller_rating,
        listing_type=listing_type,
        condition=condition,
        location=location,
        delivery=delivery,
        category=category,
        end_date=end_date,
    )


# ─── HTML fallback for individual listing pages ───────────────────────────────

def _listing_from_html(html: str, listing_id: str, url: str) -> Optional[Listing]:
    has_sofort = bool(re.search(r"sofort.{0,2}kauf", html, re.I))
    if not has_sofort:
        return None

    soup = BeautifulSoup(html, "lxml")

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

    posted_at: Optional[datetime] = None
    for node in soup.find_all(string=re.compile(r"\d{1,2}\.\s+\w+\.?\s+\d{4}", re.I)):
        dt = _parse_german_datetime(str(node))
        if dt:
            posted_at = dt
            break
    if not posted_at:
        for t_tag in soup.find_all("time"):
            da = t_tag.get("datetime") or t_tag.get_text(strip=True)
            if da:
                dt = _try_parse_date(da)
                if dt:
                    posted_at = dt
                    break

    price: Optional[float] = None
    for label_text in (
        "Sofort-Kaufpreis", "sofortkaufpreis", "Sofort Kaufpreis",
        "Sofort-Kauf", "Buy Now Price",
    ):
        label = soup.find(string=re.compile(re.escape(label_text), re.I))
        if label:
            container = label.find_parent()
            if container:
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
        for tag in soup.find_all(class_=re.compile(r"price|preis|kaufpreis", re.I)):
            candidate = tag.get_text(" ", strip=True)
            if re.search(r"start|auction|gebot|bieten", candidate, re.I):
                continue
            p = _parse_price(candidate)
            if p:
                price = p
                break

    seller_name = ""
    seller_url = ""
    vk_node = soup.find(string=re.compile(r"Verk[äa]ufer", re.I))
    if vk_node:
        container = vk_node.find_parent()
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
        for lnk in soup.find_all("a", href=re.compile(r"/de/shop/")):
            m = re.search(r"/de/shop/([^/]+)/", lnk.get("href", ""))
            if m:
                seller_name = m.group(1)
                break

    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

    image_url = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http") and re.search(r"ricardo|cdn|media|img", src, re.I):
            image_url = src
            break

    # location
    location = ""
    page_text = soup.get_text(" ", strip=True)
    m_loc = re.search(r"Standort[:\s]+([A-Za-zÀ-ÿ\s\-]+\d{4})", page_text)
    if m_loc:
        location = m_loc.group(1).strip()

    # condition
    condition = ""
    m_cond = re.search(r"Zustand[:\s]+(Neu|Gebraucht|Wie neu|Defekt)", page_text, re.I)
    if m_cond:
        condition = m_cond.group(1).strip()

    return Listing(
        listing_id=listing_id,
        title=title,
        price=price,
        url=url,
        image_url=image_url,
        posted_at=posted_at,
        seller_name=seller_name,
        seller_url=seller_url,
        listing_type="Sofortkauf",
        condition=condition,
        location=location,
    )


# ─── Seller profile ───────────────────────────────────────────────────────────

async def fetch_seller_info(
    session: aiohttp.ClientSession,
    seller_url: str,
) -> tuple[Optional[datetime], Optional[int], Optional[int]]:
    if not seller_url:
        return None, None, None
    ratings_url = (
        seller_url if "/ratings" in seller_url
        else seller_url.rstrip("/") + "/ratings/"
    )
    try:
        async with session.get(
            ratings_url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                logger.debug("Seller page {} → HTTP {}", ratings_url, resp.status)
                return None, None, None
            html = await resp.text()
    except Exception as exc:
        logger.debug("Seller profile fetch error ({}): {}", ratings_url, exc)
        return None, None, None

    reg_date: Optional[datetime] = None
    sold_count: Optional[int] = None
    purchases_count: Optional[int] = None

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

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)

    if not reg_date:
        m = re.search(r"Mitglied\s+seit\s+(\d{2}\.\d{2}\.(\d{4})|\d{4})", page_text, re.I)
        if m:
            full = m.group(1)
            if "." in full:
                parts = full.split(".")
                try:
                    reg_date = datetime(int(parts[2]), int(parts[1]), int(parts[0]),
                                        tzinfo=timezone.utc)
                except (ValueError, IndexError):
                    pass
            else:
                reg_date = datetime(int(full), 1, 1, tzinfo=timezone.utc)

    if not reg_date:
        m = re.search(r"seit\s+(20\d{2}|19\d{2})\b", page_text, re.I)
        if m:
            reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)

    if sold_count is None:
        m = re.search(r"als?\s+Verk[äa]ufer[^\d]*(\d[\d'.\s]*)", page_text, re.I)
        if m:
            try:
                sold_count = int(re.sub(r"['\s.]", "", m.group(1)))
            except ValueError:
                pass

    if purchases_count is None:
        m = re.search(r"als?\s+K[äa]ufer[^\d]*(\d[\d'.\s]*)", page_text, re.I)
        if m:
            try:
                purchases_count = int(re.sub(r"['\s.]", "", m.group(1)))
            except ValueError:
                pass

    return reg_date, sold_count, purchases_count


# ─── Individual listing fetch ─────────────────────────────────────────────────

async def fetch_listing_detail(
    session: aiohttp.ClientSession,
    listing_id: str,
) -> Optional[Listing]:
    url = f"https://www.ricardo.ch/de/a/{listing_id}/"
    try:
        async with session.get(
            url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None
            final_url = str(resp.url)
            if "/a/" not in final_url and "ricardo" in final_url:
                return None
            html = await resp.text()
    except Exception as exc:
        logger.debug("fetch_listing_detail({}) network error: {}", listing_id, exc)
        return None

    nd = _extract_next_data(html)
    if nd:
        listing = _listing_from_next_data(nd, listing_id, url)
        if listing:
            return listing

    return _listing_from_html(html, listing_id, url)


async def fetch_listing_detail_tracked(
    session: aiohttp.ClientSession,
    listing_id: str,
    stats: dict,
    debug_save: list,
) -> Optional[Listing]:
    url = f"https://www.ricardo.ch/de/a/{listing_id}/"
    html = None
    try:
        async with session.get(
            url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as resp:
            code = resp.status
            if code != 200:
                stats[f"http_{code}"] = stats.get(f"http_{code}", 0) + 1
                return None
            final_url = str(resp.url)
            if "/a/" not in final_url and "ricardo" in final_url:
                stats["redirect_dead"] = stats.get("redirect_dead", 0) + 1
                return None
            html = await resp.text()
    except Exception as exc:
        stats["network_err"] = stats.get("network_err", 0) + 1
        logger.debug("probe({}) error: {}", listing_id, exc)
        return None

    stats["http_200"] = stats.get("http_200", 0) + 1

    has_sofort = bool(re.search(r"sofort.{0,2}kauf", html, re.I))
    if not has_sofort:
        stats["no_sofort"] = stats.get("no_sofort", 0) + 1
        return None

    nd = _extract_next_data(html)
    if nd:
        listing = _listing_from_next_data(nd, listing_id, url)
        if listing:
            return listing
        stats["json_no_article"] = stats.get("json_no_article", 0) + 1

    listing = _listing_from_html(html, listing_id, url)
    if not listing:
        stats["html_no_parse"] = stats.get("html_no_parse", 0) + 1
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


# ─── Keyword search ───────────────────────────────────────────────────────────

async def search_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str] | None = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    listing_type: Optional[str] = None,
    condition: Optional[str] = None,
    n: int = 20,
) -> list[Listing]:
    """Search Ricardo.ch by keywords and return found listings."""
    if not keywords:
        return []

    keyword_str = " ".join(keywords)
    encoded = urllib.parse.quote(keyword_str)
    url = f"https://www.ricardo.ch/de/s/{encoded}/?sort=newest"
    if min_price is not None:
        url += f"&priceFrom={int(min_price)}"
    if max_price is not None:
        url += f"&priceTo={int(max_price)}"

    logger.info("🔍 Поиск по ключевым словам: {} → {}", keyword_str, url)

    try:
        async with session.get(
            url,
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.warning("Search page returned HTTP {}", resp.status)
                return []
            html = await resp.text()
    except Exception as exc:
        logger.error("search_listings network error: {}", exc)
        return []

    # Extract listing IDs from links
    found_ids: list[str] = []
    # Try to extract from __NEXT_DATA__ first
    nd = _extract_next_data(html)
    if nd:
        try:
            pp = _deep_get(nd, "props", "pageProps") or {}
            for key in ("articles", "listings", "items", "results", "data"):
                items = pp.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            lid = str(item.get("id") or item.get("articleId") or "")
                            if lid.isdigit() and lid not in found_ids:
                                found_ids.append(lid)
                    if found_ids:
                        break
            # Try dehydratedState
            if not found_ids:
                dstate = pp.get("dehydratedState") or {}
                for q in (dstate.get("queries") or []):
                    data = _deep_get(q, "state", "data")
                    if isinstance(data, dict):
                        for key in ("articles", "listings", "items", "results"):
                            items = data.get(key)
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict):
                                        lid = str(item.get("id") or item.get("articleId") or "")
                                        if lid.isdigit() and lid not in found_ids:
                                            found_ids.append(lid)
        except Exception as exc:
            logger.debug("search_listings JSON parse error: {}", exc)

    # Fallback: extract IDs from HTML links
    if not found_ids:
        for m in re.finditer(r'/de/a/(\d{8,12})/', html):
            lid = m.group(1)
            if lid not in found_ids:
                found_ids.append(lid)

    if not found_ids:
        # Try article-card data-testid patterns in HTML
        soup = BeautifulSoup(html, "lxml")
        for card in soup.find_all(attrs={"data-testid": re.compile(r"article-card", re.I)}):
            lnk = card.find("a", href=re.compile(r"/de/a/\d+"))
            if lnk:
                m = re.search(r"/de/a/(\d+)/", lnk.get("href", ""))
                if m and m.group(1) not in found_ids:
                    found_ids.append(m.group(1))

    logger.info("🔗 Найдено {} ID объявлений в поиске", len(found_ids))

    # Fetch detail pages for found IDs (up to n)
    found_ids = found_ids[:n]
    results: list[Listing] = []
    stats: dict = {}
    debug_save: list = []

    sem = asyncio.Semaphore(5)

    async def fetch_one(lid: str) -> None:
        async with sem:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            listing = await fetch_listing_detail_tracked(session, lid, stats, debug_save)
            if listing:
                results.append(listing)

    await asyncio.gather(*[fetch_one(lid) for lid in found_ids], return_exceptions=True)
    logger.info("✅ Получено {} объявлений из поиска", len(results))
    return results


# ─── Probe batch ──────────────────────────────────────────────────────────────

async def probe_batch(
    session: aiohttp.ClientSession,
    n: int = LISTING_PROBE_BATCH,
    user_filters: Optional[dict] = None,
) -> list[Listing]:
    """Probe *n* random listing IDs and return those with SOFORT KAUFEN."""
    ids = [
        str(random.randint(LISTING_ID_PREFIX_MIN, LISTING_ID_PREFIX_MAX))
        for _ in range(n)
    ]

    sem = asyncio.Semaphore(LISTING_PROBE_CONCURRENCY)
    results: list[Listing] = []
    stats: dict = {}
    debug_save: list = []

    logger.info("🔍 Проверяем {} ID в диапазоне [131xxxxxxx]", n)

    async def probe_one(lid: str) -> None:
        async with sem:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            listing = await fetch_listing_detail_tracked(session, lid, stats, debug_save)
            if listing:
                # Apply listing_type filter if provided
                if user_filters and user_filters.get("listing_type"):
                    ft = user_filters["listing_type"].lower()
                    if ft not in ("все", "all", "") and listing.listing_type:
                        if ft not in listing.listing_type.lower():
                            return
                logger.info("✅ Найдено: [{}] {} (CHF {:.0f})",
                            lid, listing.title, listing.price or 0)
                results.append(listing)

    await asyncio.gather(*[probe_one(lid) for lid in ids], return_exceptions=True)

    parts = []
    total_200 = stats.get("http_200", 0)
    parts.append(f"200: {total_200}")
    for code in (404, 403, 301, 302):
        v = stats.get(f"http_{code}", 0)
        if v:
            parts.append(f"{code}: {v}")
    if stats.get("redirect_dead"):
        parts.append(f"redirect: {stats['redirect_dead']}")
    if stats.get("network_err"):
        parts.append(f"err: {stats['network_err']}")
    if stats.get("no_sofort"):
        parts.append(f"no-sofort: {stats['no_sofort']}")
    if stats.get("json_no_article"):
        parts.append(f"json-miss: {stats['json_no_article']}")
    if stats.get("html_no_parse"):
        parts.append(f"html-miss: {stats['html_no_parse']}")

    logger.info("📊 Батч завершён: {}/{} найдено | {}", len(results), n, ", ".join(parts))
    return results


# ─── Combined search entry point ─────────────────────────────────────────────

async def probe_or_search(
    session: aiohttp.ClientSession,
    user_filters: dict,
    n: int = 50,
) -> list[Listing]:
    """
    Use keyword search if keywords are set, otherwise fall back to probe_batch.
    Returns a deduplicated list of Listing objects.
    """
    keywords = user_filters.get("keywords") or []
    categories = user_filters.get("categories") or []

    if keywords:
        results = await search_listings(
            session,
            keywords=keywords,
            categories=categories if categories else None,
            min_price=user_filters.get("min_price"),
            max_price=user_filters.get("max_price"),
            listing_type=user_filters.get("listing_type"),
            condition=user_filters.get("condition"),
            n=n,
        )
    else:
        results = await probe_batch(session, n=n, user_filters=user_filters)

    # Deduplicate by listing_id
    seen_ids: set[str] = set()
    deduped: list[Listing] = []
    for lst in results:
        if lst.listing_id not in seen_ids:
            seen_ids.add(lst.listing_id)
            deduped.append(lst)
    return deduped


# ─── Legacy API compat ────────────────────────────────────────────────────────

async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Legacy wrapper – kept for backward compatibility."""
    return await probe_batch(session)
