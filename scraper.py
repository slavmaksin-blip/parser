"""Async scraper for ricardo.ch listings."""

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, urljoin

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
}

# ─── Category definitions ────────────────────────────────────────────────────
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

CATEGORY_URLS: dict[str, str] = {
    "hats":              "https://www.ricardo.ch/de/c/huete-muetzen-caps-fuer-damen-73899/",
    "shoes_men":         "https://www.ricardo.ch/de/c/herrenschuhe-40822/?attribute_groups.shoe_type=120",
    "wedding":           "https://www.ricardo.ch/de/c/hochzeit-hochzeitsdeko-zubehoer-40836/",
    "backpacks":         "https://www.ricardo.ch/de/c/ruecksaecke-63791/",
    "folk":              "https://www.ricardo.ch/de/c/trachtenmode-40839/",
    "clothing":          "https://www.ricardo.ch/de/c/kleidung-accessoires-40842/",
    "blouses":           "https://www.ricardo.ch/de/c/blusen-und-tunika-40780/",
    "accessories_women": "https://www.ricardo.ch/de/c/accessoires-fuer-damen-40749/",
}

# ─── Random-probe constants ───────────────────────────────────────────────────
# IDs observed in the problem statement: 1307375512, 1313109388 (~1.3 billion range)
LISTING_ID_MIN = 1_300_000_000
LISTING_ID_MAX = 1_350_000_000
LISTING_PROBE_COUNT = 60          # random IDs to probe per monitoring run
LISTING_PROBE_CONCURRENCY = 10    # max simultaneous HTTP requests

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
        seller_reg_before = filters.get("max_seller_reg_date")  # ISO date string
        listing_from = filters.get("listing_date_from")  # ISO datetime string
        listing_to = filters.get("listing_date_to")  # ISO datetime string
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
        if self.category:
            lines.append(f"🏷 Категория: {self.category}")
        return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> Optional[float]:
    """Extract a float price from a Swiss-formatted price string."""
    text = text.strip().replace("\xa0", " ")
    match = re.search(r"[\d'., ]+", text)
    if not match:
        return None
    raw = match.group(0).replace("'", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_relative_date(text: str) -> Optional[datetime]:
    """Convert ricardo relative date strings to datetime (best-effort)."""
    text = text.strip().lower()
    now = datetime.now(timezone.utc)
    if "minute" in text:
        m = re.search(r"(\d+)", text)
        minutes = int(m.group(1)) if m else 5
        return now - timedelta(minutes=minutes)
    if "stunde" in text or "hour" in text:
        m = re.search(r"(\d+)", text)
        hours = int(m.group(1)) if m else 1
        return now - timedelta(hours=hours)
    if "gestern" in text or "yesterday" in text:
        return now - timedelta(days=1)
    if "heute" in text or "today" in text:
        return now
    # Try explicit date like "03.04.2025"
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _parse_german_datetime(text: str) -> Optional[datetime]:
    """Parse Ricardo.ch listing date format: '8. Apr. 2026, 17:45 Uhr'."""
    text = text.strip()
    # Primary pattern with time: "8. Apr. 2026, 17:45 Uhr"
    m = re.search(
        r"(\d{1,2})\.\s+(\w+\.?)\s+(\d{4})[,\s]+(\d{1,2}):(\d{2})",
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
    # Date only: "8. Apr. 2026"
    m = re.search(r"(\d{1,2})\.\s+(\w+\.?)\s+(\d{4})", text, re.I)
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


def _add_page_param(base_url: str, page: int) -> str:
    """Append page=N to a category URL, handling existing query strings."""
    if page <= 1:
        return base_url
    parsed = urlparse(base_url)
    qs = parsed.query
    if qs:
        return base_url + f"&page={page}"
    return base_url.rstrip("/") + f"/?page={page}"


# ─── Seller profile ───────────────────────────────────────────────────────────

async def fetch_seller_info(
    session: aiohttp.ClientSession, seller_url: str
) -> tuple[Optional[datetime], Optional[int], Optional[int]]:
    """Fetch seller profile and return (registration_date, sold_count, purchases_count)."""
    if not seller_url:
        return None, None, None
    # Normalise URL to the ratings page
    ratings_url = seller_url
    if "/ratings" not in ratings_url:
        ratings_url = seller_url.rstrip("/") + "/ratings/"
    try:
        async with session.get(ratings_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None, None, None
            html = await resp.text()
        soup = BeautifulSoup(html, "lxml")

        reg_date: Optional[datetime] = None
        sold_count: Optional[int] = None
        purchases_count: Optional[int] = None

        # Registration date: look for "Mitglied seit" text
        for tag in soup.find_all(string=re.compile(r"Mitglied seit|member since", re.I)):
            parent = tag.parent
            text = parent.get_text(" ", strip=True)
            # Full date: "15.01.2018"
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if m:
                reg_date = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
                break
            # Year only: "Mitglied seit 2018"
            m = re.search(r"\b(20\d{2}|19\d{2})\b", text)
            if m:
                reg_date = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
                break

        # Sold count: look for patterns like "123 Bewertungen als Verkäufer" / "Verkäufe"
        for tag in soup.find_all(string=re.compile(r"Verk[äa]ufer|Verk[äa]ufe|as seller", re.I)):
            parent = tag.parent
            text = parent.get_text(" ", strip=True)
            m = re.search(r"(\d[\d\s']*)", text)
            if m:
                try:
                    sold_count = int(m.group(1).replace(" ", "").replace("'", ""))
                    break
                except ValueError:
                    pass

        # Purchases count: look for "als Käufer" / "Käufe"
        for tag in soup.find_all(string=re.compile(r"K[äa]ufer|K[äa]ufe|as buyer", re.I)):
            parent = tag.parent
            text = parent.get_text(" ", strip=True)
            m = re.search(r"(\d[\d\s']*)", text)
            if m:
                try:
                    purchases_count = int(m.group(1).replace(" ", "").replace("'", ""))
                    break
                except ValueError:
                    pass

        # Fallback: look for stat numbers in data-testid attributes
        if sold_count is None:
            for tag in soup.select("[data-testid*='seller-rating'], [data-testid*='sold'], [class*='sold']"):
                m = re.search(r"(\d+)", tag.get_text())
                if m:
                    sold_count = int(m.group(1))
                    break

        return reg_date, sold_count, purchases_count
    except Exception as exc:
        logger.debug("seller profile fetch error: %s", exc)
    return None, None, None


# ─── Listing detail page ────────────────────────────────────────────────────

async def fetch_listing_detail(
    session: aiohttp.ClientSession, listing_id: str
) -> Optional["Listing"]:
    """Fetch a single listing page by ID and return a Listing if it has SOFORT KAUFEN."""
    url = f"https://www.ricardo.ch/de/a/{listing_id}/"
    try:
        async with session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None
            # If redirect took us away from the article path, it's a dead ID
            if "/a/" not in str(resp.url):
                return None
            html = await resp.text()
    except Exception as exc:
        logger.debug("fetch_listing_detail(%s) error: %s", listing_id, exc)
        return None

    soup = BeautifulSoup(html, "lxml")

    # ── Must have a "SOFORT KAUFEN" button ────────────────────────────────
    sofort_btn = soup.find(
        string=re.compile(r"sofort\s*kaufen", re.I)
    ) or soup.find(
        attrs={"data-testid": re.compile(r"buy.now|sofort", re.I)}
    )
    if not sofort_btn:
        return None

    # ── Title ─────────────────────────────────────────────────────────────
    title: Optional[str] = None
    for candidate in [
        soup.find("h1"),
        soup.find(attrs={"data-testid": re.compile(r"title|name", re.I)}),
        soup.find(class_=re.compile(r"title|heading|product.name", re.I)),
    ]:
        if candidate:
            t = candidate.get_text(strip=True)
            if t:
                title = t
                break
    if not title:
        return None

    # ── Publication date ──────────────────────────────────────────────────
    # Ricardo shows "8. Apr. 2026, 17:45 Uhr" near the top of the page
    posted_at: Optional[datetime] = None
    for text_node in soup.find_all(string=re.compile(r"\d{1,2}\.\s+\w+\s+\d{4}", re.I)):
        dt = _parse_german_datetime(str(text_node))
        if dt:
            posted_at = dt
            break
    if not posted_at:
        for time_tag in soup.find_all("time"):
            dt_attr = time_tag.get("datetime", "")
            if dt_attr:
                try:
                    posted_at = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                    break
                except ValueError:
                    pass

    # ── Price (Sofort-Kaufpreis) ───────────────────────────────────────────
    price: Optional[float] = None
    price_label = soup.find(string=re.compile(r"Sofort.Kaufpreis|sofortkaufpreis", re.I))
    if price_label:
        # price is usually in the next sibling element or a close parent
        container = price_label.find_parent()
        if container:
            for sibling in list(container.next_siblings) + [container.parent]:
                if sibling and hasattr(sibling, "get_text"):
                    p = _parse_price(sibling.get_text(" ", strip=True))
                    if p:
                        price = p
                        break
    if price is None:
        for tag in soup.find_all(class_=re.compile(r"price|preis", re.I)):
            p = _parse_price(tag.get_text())
            if p:
                price = p
                break

    # ── Seller username from "Verkäufer" section ──────────────────────────
    seller_name = ""
    seller_url = ""

    # First try: find "Verkäufer" label and look for a shop link nearby
    vk_label = soup.find(string=re.compile(r"Verk[äa]ufer", re.I))
    if vk_label:
        container = vk_label.find_parent()
        search_root = container.parent if container else soup
        if search_root:
            shop_link = search_root.find("a", href=re.compile(r"/de/shop/"))
            if shop_link:
                m = re.search(r"/de/shop/([^/]+)/", shop_link.get("href", ""))
                if m:
                    seller_name = m.group(1)

    # Fallback: any shop link on the page
    if not seller_name:
        for link in soup.find_all("a", href=re.compile(r"/de/shop/")):
            m = re.search(r"/de/shop/([^/]+)/", link.get("href", ""))
            if m:
                seller_name = m.group(1)
                break

    if seller_name:
        seller_url = f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"

    # ── Image ─────────────────────────────────────────────────────────────
    image_url = ""
    img = soup.find("img", src=re.compile(r"ricardo|cdn", re.I))
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


# ─── Main scraper ─────────────────────────────────────────────────────────────

async def probe_random_listings(
    session: aiohttp.ClientSession,
    n_probes: int = LISTING_PROBE_COUNT,
) -> list[Listing]:
    """Generate random 10-digit listing IDs and return those with SOFORT KAUFEN."""
    ids = [
        str(random.randint(LISTING_ID_MIN, LISTING_ID_MAX))
        for _ in range(n_probes)
    ]
    semaphore = asyncio.Semaphore(LISTING_PROBE_CONCURRENCY)
    results: list[Listing] = []

    async def probe_one(lid: str) -> None:
        async with semaphore:
            listing = await fetch_listing_detail(session, lid)
            if listing:
                results.append(listing)
            await asyncio.sleep(0.3)

    await asyncio.gather(*[probe_one(lid) for lid in ids], return_exceptions=True)
    return results


async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Probe random Ricardo.ch listing IDs and return SOFORT KAUFEN listings.

    keywords / categories are accepted for API compatibility but are not used
    for navigation – filtering by these is handled in Listing.matches().
    """
    return await probe_random_listings(session, n_probes=LISTING_PROBE_COUNT)


async def enrich_seller_info(
    session: aiohttp.ClientSession, listings: list[Listing]
) -> None:
    """Fetch seller registration dates and stats for listings that have a seller URL."""
    tasks = []
    for listing in listings:
        if listing.seller_url:
            tasks.append(_enrich_one(session, listing))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _enrich_one(session: aiohttp.ClientSession, listing: Listing) -> None:
    reg, sold, purchases = await fetch_seller_info(session, listing.seller_url)
    listing.seller_registered = reg
    listing.sold_count = sold
    listing.purchases_count = purchases
