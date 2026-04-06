"""Async scraper for ricardo.ch listings."""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs, urljoin

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
# key → (Russian name, base URL)
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

# Number of pages to scan per category per monitoring run
PAGES_PER_CATEGORY = 2


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
            self.seller_registered.strftime("%d.%m.%Y %H:%M")
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


def _add_page_param(base_url: str, page: int) -> str:
    """Append page=N to a category URL, handling existing query strings."""
    if page <= 1:
        return base_url
    parsed = urlparse(base_url)
    qs = parsed.query
    if qs:
        return base_url + f"&page={page}"
    # URL ends with '/' – use '?page=N'
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
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if m:
                reg_date = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
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


# ─── Main scraper ─────────────────────────────────────────────────────────────

async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Fetch new listings from Ricardo.ch for given keywords and categories."""
    results: list[Listing] = []
    seen_ids: set[str] = set()

    # Determine which category keys to query
    cat_keys: list[str]
    if not categories:
        cat_keys = list(CATEGORY_URLS.keys())
    else:
        cat_keys = [c for c in categories if c in CATEGORY_URLS]
        if not cat_keys:
            cat_keys = list(CATEGORY_URLS.keys())

    for cat_key in cat_keys:
        base_url = CATEGORY_URLS[cat_key]
        cat_name = CATEGORIES[cat_key]

        for page in range(1, PAGES_PER_CATEGORY + 1):
            url = _add_page_param(base_url, page)

            # Append keyword search param if provided
            params: dict = {}
            if keywords:
                params["q"] = " ".join(keywords)

            try:
                async with session.get(
                    url,
                    params=params if params else None,
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("ricardo.ch returned %s for %s", resp.status, url)
                        break
                    html = await resp.text()
            except Exception as exc:
                logger.error("Error fetching %s: %s", url, exc)
                break

            page_listings = _parse_search_page(html, cat_name)
            added = 0
            for listing in page_listings:
                if listing.listing_id not in seen_ids:
                    seen_ids.add(listing.listing_id)
                    results.append(listing)
                    added += 1

            # If no new listings on this page, stop paginating this category
            if added == 0:
                break

            await asyncio.sleep(1)

    return results


def _parse_search_page(html: str, category_name: str = "") -> list[Listing]:
    """Parse listing cards from the Ricardo search results HTML."""
    soup = BeautifulSoup(html, "lxml")
    listings: list[Listing] = []

    # Ricardo renders article cards; selectors may need updating if the site changes.
    # We try several common patterns.
    cards = (
        soup.select("article[data-testid]")
        or soup.select("article.listing-card")
        or soup.select("[data-testid='listing-card']")
        or soup.select("li[class*='listing']")
        or soup.select("div[class*='ArticleCard']")
        or soup.select("a[href*='/a/']")  # fallback: any link to an article
    )

    for card in cards:
        listing = _parse_card(card, category_name)
        if listing:
            listings.append(listing)

    return listings


def _parse_card(card, category_name: str) -> Optional[Listing]:
    """Extract a single Listing from an HTML card element."""
    try:
        # ── URL & ID ───────────────────────────────────────────────────────
        link = card if card.name == "a" else card.find("a", href=True)
        if not link:
            return None
        href = link.get("href", "")
        if not href:
            return None
        if not href.startswith("http"):
            href = "https://www.ricardo.ch" + href

        # Extract listing ID from URL patterns like /a/12345678/ or similar
        id_match = re.search(r"/a/(\d+)/", href) or re.search(r"/(\d{6,})", href)
        if not id_match:
            return None
        listing_id = id_match.group(1)

        # ── Title ──────────────────────────────────────────────────────────
        title_tag = (
            card.find(attrs={"data-testid": re.compile(r"title", re.I)})
            or card.find(["h2", "h3", "h4"])
            or card.find(class_=re.compile(r"title|name", re.I))
        )
        title = title_tag.get_text(strip=True) if title_tag else link.get_text(strip=True)
        if not title:
            return None

        # ── Price ──────────────────────────────────────────────────────────
        price_tag = (
            card.find(attrs={"data-testid": re.compile(r"price", re.I)})
            or card.find(class_=re.compile(r"price|preis", re.I))
        )
        price = _parse_price(price_tag.get_text()) if price_tag else None

        # ── Image ──────────────────────────────────────────────────────────
        img_tag = card.find("img")
        image_url = ""
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-src") or ""

        # ── Posted date ────────────────────────────────────────────────────
        date_tag = card.find(
            attrs={"data-testid": re.compile(r"date|time", re.I)}
        ) or card.find("time") or card.find(class_=re.compile(r"date|time|ago", re.I))
        posted_at: Optional[datetime] = None
        if date_tag:
            dt_attr = date_tag.get("datetime")
            if dt_attr:
                try:
                    posted_at = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                except ValueError:
                    pass
            if not posted_at:
                posted_at = _parse_relative_date(date_tag.get_text(strip=True))

        # ── Seller ─────────────────────────────────────────────────────────
        seller_tag = card.find(
            attrs={"data-testid": re.compile(r"seller|vendor", re.I)}
        ) or card.find(class_=re.compile(r"seller|vendor|user", re.I))
        seller_name = seller_tag.get_text(strip=True) if seller_tag else ""
        seller_href = seller_tag.get("href", "") if seller_tag and seller_tag.name == "a" else ""
        if seller_href and not seller_href.startswith("http"):
            seller_href = "https://www.ricardo.ch" + seller_href

        return Listing(
            listing_id=listing_id,
            title=title,
            price=price,
            url=href,
            image_url=image_url,
            category=category_name,
            posted_at=posted_at,
            seller_name=seller_name,
            seller_url=seller_href,
        )
    except Exception as exc:
        logger.debug("Error parsing card: %s", exc)
        return None


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
