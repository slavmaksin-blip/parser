"""Async scraper for ricardo.ch listings."""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

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

# Base URL for ricardo.ch search
SEARCH_URL = "https://www.ricardo.ch/de/suche/"

# ─── Category definitions ────────────────────────────────────────────────────
CATEGORIES: dict[str, str] = {
    "all": "Alle Kategorien",
    "antiques": "Antiquitäten & Kunst",
    "auto": "Auto & Zubehör",
    "baby": "Baby & Kind",
    "books": "Bücher & Comics",
    "computers": "Computer & Zubehör",
    "electronics": "Elektronik & Foto",
    "fashion": "Mode & Kleidung",
    "home": "Heim & Garten",
    "hobby": "Hobby & Freizeit",
    "music": "Musik, Filme & Spiele",
    "sports": "Sport & Outdoor",
    "tickets": "Tickets & Gutscheine",
    "other": "Sonstiges",
}

# Mapping from category key to Ricardo URL slug
CATEGORY_SLUGS: dict[str, str] = {
    "antiques": "antiquitaten-kunst",
    "auto": "auto-zubehor",
    "baby": "baby-kind",
    "books": "bucher-comics-zeitschriften",
    "computers": "computer-zubehor",
    "electronics": "elektronik-foto",
    "fashion": "mode-kleidung",
    "home": "heim-garten",
    "hobby": "hobby-freizeit",
    "music": "musik-filme-spiele",
    "sports": "sport-outdoor",
    "tickets": "tickets-gutscheine",
    "other": "sonstiges",
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
    description: str = ""

    def matches(self, filters: dict) -> bool:
        """Return True if this listing passes all active filters."""
        min_p = filters.get("min_price")
        max_p = filters.get("max_price")
        max_age_h = filters.get("max_listing_age_h")
        max_reg = filters.get("max_seller_reg_date")  # ISO date string

        if min_p is not None and self.price is not None and self.price < min_p:
            return False
        if max_p is not None and self.price is not None and self.price > max_p:
            return False

        if max_age_h is not None and self.posted_at is not None:
            now = datetime.now(timezone.utc)
            posted = self.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            age_h = (now - posted).total_seconds() / 3600
            if age_h > max_age_h:
                return False

        if max_reg and self.seller_registered:
            max_dt = datetime.fromisoformat(max_reg)
            reg = self.seller_registered
            if reg.tzinfo is None:
                reg = reg.replace(tzinfo=timezone.utc)
            if max_dt.tzinfo is None:
                max_dt = max_dt.replace(tzinfo=timezone.utc)
            if reg > max_dt:
                return False

        return True

    def format_message(self) -> str:
        price_str = f"CHF {self.price:.2f}" if self.price is not None else "Preis auf Anfrage"
        posted_str = (
            self.posted_at.strftime("%d.%m.%Y %H:%M")
            if self.posted_at
            else "Unbekannt"
        )
        reg_str = (
            self.seller_registered.strftime("%d.%m.%Y")
            if self.seller_registered
            else "Unbekannt"
        )
        lines = [
            f"🛍 <b>{self.title}</b>",
            f"💰 {price_str}",
            f"📅 Eingestellt: {posted_str}",
            f"👤 Verkäufer: {self.seller_name or 'Unbekannt'} (Mitglied seit {reg_str})",
        ]
        if self.category:
            lines.append(f"🏷 Kategorie: {self.category}")
        lines.append(f"🔗 <a href=\"{self.url}\">Zur Anzeige</a>")
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


# ─── Seller profile ───────────────────────────────────────────────────────────

async def fetch_seller_registration(
    session: aiohttp.ClientSession, seller_url: str
) -> Optional[datetime]:
    """Fetch seller profile and extract registration date."""
    if not seller_url:
        return None
    try:
        async with session.get(seller_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
        soup = BeautifulSoup(html, "lxml")
        # Look for "Mitglied seit" text
        for tag in soup.find_all(string=re.compile(r"Mitglied seit|member since", re.I)):
            parent = tag.parent
            text = parent.get_text(" ", strip=True)
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
            if m:
                return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
    except Exception as exc:
        logger.debug("seller profile fetch error: %s", exc)
    return None


# ─── Main scraper ─────────────────────────────────────────────────────────────

async def fetch_listings(
    session: aiohttp.ClientSession,
    keywords: list[str],
    categories: list[str],
) -> list[Listing]:
    """Fetch new listings from Ricardo.ch for given keywords and categories."""
    results: list[Listing] = []
    seen_ids: set[str] = set()

    # Determine which category slugs to query
    slugs: list[Optional[str]] = []
    if not categories or "all" in categories:
        slugs = [None]  # no category filter
    else:
        for cat in categories:
            slug = CATEGORY_SLUGS.get(cat)
            slugs.append(slug)

    # If no keywords, use a broad empty query
    query_terms = keywords if keywords else [""]

    for query in query_terms:
        for slug in slugs:
            params: dict = {
                "q": query,
                "sort": "newest",
            }
            url = SEARCH_URL
            if slug:
                url = f"https://www.ricardo.ch/de/c/{slug}/"
                if query:
                    params["q"] = query

            try:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("ricardo.ch returned %s for %s", resp.status, url)
                        continue
                    html = await resp.text()
            except Exception as exc:
                logger.error("Error fetching %s: %s", url, exc)
                continue

            listings = _parse_search_page(html, CATEGORIES.get(slug or "all", ""))
            for listing in listings:
                if listing.listing_id not in seen_ids:
                    seen_ids.add(listing.listing_id)
                    results.append(listing)

            # Polite delay between requests
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
    """Fetch seller registration dates for listings that have a seller URL."""
    tasks = []
    for listing in listings:
        if listing.seller_url:
            tasks.append(_enrich_one(session, listing))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _enrich_one(session: aiohttp.ClientSession, listing: Listing) -> None:
    listing.seller_registered = await fetch_seller_registration(
        session, listing.seller_url
    )
