"""Async scraper for ricardo.ch listings using Playwright (single search approach).

Builds a search URL from user filters, loads the page with a real browser,
extracts article cards from the DOM, and optionally enriches seller data.
"""

import asyncio
import json
import os
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any

import aiohttp
from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
)
from pydantic import BaseModel

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

# Optional: playwright-stealth for stronger fingerprint masking
try:
    from playwright_stealth import stealth_async as _stealth_async  # type: ignore
    _HAS_PLAYWRIGHT_STEALTH = True
except ImportError:
    _HAS_PLAYWRIGHT_STEALTH = False


# ─── Stealth JS injected into every page via context.add_init_script() ────────
# Removes the most common Playwright/CDP automation signals that Cloudflare
# and other bot-detection systems check for.
_STEALTH_JS = """
(function () {
    // 1. Remove navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

    // 2. Spoof window.chrome to look like a real Chrome install
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || {};
    window.chrome.app = window.chrome.app || {};

    // 3. Fix navigator.languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['de-CH', 'de', 'en-US', 'en']
    });

    // 4. Fake non-empty plugin list (headless Chrome has no plugins)
    const fakePlugin = (name, desc, fn) => {
        return {name, description: desc, filename: fn, length: 1,
                item: (i) => null, namedItem: (n) => null, [Symbol.iterator]: function*() {}};
    };
    const fakePlugins = [
        fakePlugin('PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer'),
        fakePlugin('Chrome PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer'),
        fakePlugin('Chromium PDF Viewer', 'Portable Document Format', 'mhjfbmdgcfjbbpaeojofohoefgiehjai'),
    ];
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign(fakePlugins, {length: fakePlugins.length,
            item: (i) => fakePlugins[i] || null,
            namedItem: (n) => fakePlugins.find(p => p.name === n) || null,
            [Symbol.iterator]: function*() { yield* fakePlugins; }
        })
    });

    // 5. Fix permissions.query for notifications (headless returns 'denied' which is a signal)
    if (navigator.permissions && navigator.permissions.query) {
        const _origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => {
            if (params && params.name === 'notifications') {
                return Promise.resolve({state: 'default', onchange: null});
            }
            return _origQuery(params);
        };
    }

    // 6. Realistic connection info
    try {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({effectiveType: '4g', rtt: 50, downlink: 10,
                         saveData: false, type: 'wifi', onchange: null})
        });
    } catch (_) {}

    // 7. Remove Playwright-specific properties from Error stacks
    const _origError = Error;
    window.Error = class extends _origError {};
})();
"""


def _headers() -> dict:
    return {
        "User-Agent": _random_ua(),
        "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


# ─── Category definitions ─────────────────────────────────────────────────────
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

# German month names / abbreviations → int
_DE_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "mar": 3,
    "apr": 4, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "dez": 12,
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
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


# ─── Pydantic model ───────────────────────────────────────────────────────────

class RicardoAd(BaseModel):
    article_id: str
    title: str
    price: float
    currency: str = "CHF"
    link: str
    image_urls: list[str] = []
    creation_date: Optional[datetime] = None
    seller_username: str = ""
    seller_registration_date: Optional[datetime] = None
    seller_sales_count: int = 0
    seller_rating: Optional[float] = None
    location: str = ""
    category: str = ""
    shipping: Optional[str] = None
    raw_data: dict = {}


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


# ─── JSON helpers (used by aiohttp seller enrichment) ────────────────────────

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


# ─── Browser state ────────────────────────────────────────────────────────────

_playwright_instance: Any = None
_browser: Any = None
_browser_context: Optional[BrowserContext] = None
_consecutive_errors: int = 0
MAX_CONSECUTIVE_ERRORS: int = 5  # restart browser after this many consecutive failures


async def init_browser() -> BrowserContext:
    """Initialize (or re-initialize) a hardened headless Chromium browser context."""
    global _playwright_instance, _browser, _browser_context
    for obj, method in [
        (_browser_context, "close"),
        (_browser, "close"),
        (_playwright_instance, "stop"),
    ]:
        if obj is not None:
            try:
                await getattr(obj, method)()
            except Exception:
                pass
    _browser_context = None
    _browser = None
    _playwright_instance = None

    _playwright_instance = await async_playwright().start()
    _browser = await _playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # Hide automation signals
            "--disable-blink-features=AutomationControlled",
            "--disable-automation",
            "--disable-infobars",
            # Realistic features
            "--enable-javascript",
            "--disable-popup-blocking",
            # Reduce fingerprinting surface
            "--disable-web-security",
            "--allow-running-insecure-content",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-size=1280,800",
        ],
    )
    _browser_context = await _browser.new_context(
        user_agent=_random_ua(),
        locale="de-CH",
        timezone_id="Europe/Zurich",
        viewport={"width": 1280, "height": 800},
        screen={"width": 1280, "height": 800},
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "de-CH,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "sec-ch-ua-platform": '"Windows"',
        },
        java_script_enabled=True,
        # Disable webdriver flag at context level
        bypass_csp=False,
    )
    # Inject stealth JS into every page before any scripts run
    await _browser_context.add_init_script(script=_STEALTH_JS)
    logger.info("🌐 Playwright браузер инициализирован (stealth={})", _HAS_PLAYWRIGHT_STEALTH)
    return _browser_context


async def close_browser() -> None:
    """Shut down the Playwright browser and release resources."""
    global _playwright_instance, _browser, _browser_context
    for obj, method in [
        (_browser_context, "close"),
        (_browser, "close"),
        (_playwright_instance, "stop"),
    ]:
        if obj is not None:
            try:
                await getattr(obj, method)()
            except Exception:
                pass
    _browser_context = None
    _browser = None
    _playwright_instance = None
    logger.info("🌐 Playwright браузер закрыт")


async def _ensure_browser() -> BrowserContext:
    """Return the existing BrowserContext, initializing one if needed."""
    global _browser_context
    if _browser_context is None:
        await init_browser()
    return _browser_context  # type: ignore[return-value]


# ─── Utilities ────────────────────────────────────────────────────────────────

def rotate_user_agent() -> str:
    """Return a random User-Agent string."""
    return _random_ua()


async def random_delay(min_sec: float = 8, max_sec: float = 25) -> None:
    """Sleep for a random duration to reduce bot-detection risk."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def take_screenshot_on_error(page: Page, step_name: str) -> None:
    """Save a debug screenshot to a temporary directory on error."""
    try:
        import tempfile
        path = os.path.join(
            tempfile.gettempdir(),
            f"error_{step_name}_{int(datetime.now().timestamp())}.png",
        )
        await page.screenshot(path=path, full_page=False)
        logger.debug("📸 Screenshot saved: {}", path)
    except Exception as exc:
        logger.debug("Screenshot failed ({}): {}", step_name, exc)


# ─── Cloudflare detection ─────────────────────────────────────────────────────

class CloudflareBlockError(RuntimeError):
    """Raised when a Cloudflare challenge page is detected."""


async def _is_cloudflare_blocked(page: Page) -> bool:
    """Return True if the current page is a Cloudflare bot-challenge page."""
    try:
        title = (await page.title()).lower()
        if "just a moment" in title or "attention required" in title:
            return True
        # Check for Cloudflare challenge elements / markers in page content
        for selector in (
            "#challenge-form",
            "#cf-please-wait",
            ".cf-browser-verification",
            "[data-translate='why_captcha_headline']",
            "input[name='cf-turnstile-response']",
        ):
            if await page.query_selector(selector):
                return True
        content = await page.content()
        if "cloudflare" in content.lower() and (
            "challenge" in content.lower() or "turnstile" in content.lower()
            or "cf-chl" in content.lower()
        ):
            return True
    except Exception:
        pass
    return False


# ─── Step 1: URL builder ──────────────────────────────────────────────────────

def build_search_url(filters: dict) -> str:
    """Build a Ricardo.ch search URL from a user filter dict.

    Examples::

        https://www.ricardo.ch/de/s/iphone?sort=newest&priceFrom=200&priceTo=550
        https://www.ricardo.ch/de/s/?sort=newest&priceFrom=150
        https://www.ricardo.ch/de/s/iPhone%2014?sort=newest&shipping=Abholung
    """
    keywords: list[str] = filters.get("keywords") or []
    min_price = filters.get("min_price")
    max_price = filters.get("max_price")
    listing_type: str = filters.get("listing_type") or ""
    condition: str = filters.get("condition") or ""
    delivery: str = filters.get("delivery") or ""

    if keywords:
        kw = " ".join(keywords)
        base = f"https://www.ricardo.ch/de/s/{urllib.parse.quote(kw)}"
    else:
        base = "https://www.ricardo.ch/de/s/"

    params: dict[str, str] = {"sort": "newest"}

    if min_price:
        params["priceFrom"] = str(int(min_price))
    if max_price:
        params["priceTo"] = str(int(max_price))

    if listing_type and listing_type.lower() not in ("все", "all", ""):
        lt_map = {
            "sofortkauf": "buy_now",
            "auktion": "auction",
            "festpreis": "fixed_price",
        }
        params["saleType"] = lt_map.get(listing_type.lower(), listing_type)

    if condition and condition.lower() not in ("alle", "all", "все", ""):
        params["condition"] = condition

    if delivery and delivery.lower() not in ("beides", "all", "все", ""):
        if "abholung" in delivery.lower():
            params["shipping"] = "Abholung"
        elif "versand" in delivery.lower():
            params["shipping"] = "Versand"

    return f"{base}?{urllib.parse.urlencode(params)}"


# ─── Step 2: safe_goto ────────────────────────────────────────────────────────

async def safe_goto(page: Page, url: str, retries: int = 3) -> bool:
    """Navigate to *url* with automatic retry on failure.

    Returns True on success, False if all retries are exhausted.
    Applies playwright-stealth per-page if the library is available.
    """
    # Apply per-page stealth patch (on top of the context-level init_script)
    if _HAS_PLAYWRIGHT_STEALTH:
        try:
            await _stealth_async(page)
        except Exception:
            pass

    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            return True
        except Exception as exc:
            logger.warning(
                "safe_goto attempt {}/{} for {}: {}",
                attempt + 1, retries, url, exc,
            )
            await take_screenshot_on_error(page, f"goto_fail_{attempt + 1}")
            if attempt < retries - 1:
                await asyncio.sleep(5 * (attempt + 1))
    return False


# ─── Step 2: load_search_page ────────────────────────────────────────────────

async def load_search_page(page: Page, url: str) -> None:
    """Load the Ricardo.ch search results page and scroll to reveal all cards.

    Raises CloudflareBlockError if a Cloudflare challenge is detected so that
    the caller can sleep and restart the browser.
    """
    success = await safe_goto(page, url)
    if not success:
        await take_screenshot_on_error(page, "load_search_failed")
        raise RuntimeError(f"Failed to load search page after retries: {url}")

    # Detect Cloudflare block before waiting for article cards
    if await _is_cloudflare_blocked(page):
        await take_screenshot_on_error(page, "cloudflare_block")
        raise CloudflareBlockError(
            f"Cloudflare challenge detected on {url} — bot-detection triggered"
        )

    try:
        await page.wait_for_selector(
            '[data-testid="article-card"], .article-card',
            timeout=15_000,
        )
    except Exception as exc:
        # One last Cloudflare check — the challenge can appear after networkidle
        if await _is_cloudflare_blocked(page):
            await take_screenshot_on_error(page, "cloudflare_block_late")
            raise CloudflareBlockError(
                f"Cloudflare challenge appeared after page load on {url}"
            )
        await take_screenshot_on_error(page, "no_article_cards")
        raise RuntimeError(f"Article cards not found on {url}: {exc}")

    # Scroll down 2–3 times to trigger lazy-loading of all cards on the first page
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
        await asyncio.sleep(0.8)


# ─── Step 3: extract_cards ───────────────────────────────────────────────────

async def extract_cards(page: Page) -> list[dict]:
    """Extract raw article card data from the current search results page."""
    cards: list[dict] = []
    try:
        card_elements = await page.query_selector_all(
            '[data-testid="article-card"], .article-card'
        )
    except Exception as exc:
        logger.error("extract_cards query failed: {}", exc)
        return cards

    for card_el in card_elements:
        try:
            raw: dict = {}

            # article_id
            article_id = await card_el.get_attribute("data-article-id") or ""
            if not article_id:
                link_el = await card_el.query_selector("a[href*='/de/a/']")
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    # Ricardo article IDs are typically 8–10 digit numbers
                    m = re.search(r"/de/a/(\d{6,12})", href)
                    if m:
                        article_id = m.group(1)
            if not article_id:
                continue
            raw["article_id"] = article_id

            # title
            for sel in (
                '[data-testid="article-title"]',
                ".article-title",
                "h2",
                "h3",
            ):
                el = await card_el.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        raw["title"] = text
                        break

            # price
            for sel in ('[data-testid="price"]', ".price"):
                el = await card_el.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    raw["price_text"] = text
                    p = _parse_price(text)
                    if p:
                        raw["price"] = p
                    break

            # seller
            for sel in ('[data-testid="seller-name"]', ".seller-name"):
                el = await card_el.query_selector(sel)
                if el:
                    raw["seller_username"] = (await el.inner_text()).strip()
                    break

            # location
            for sel in ('[data-testid="location"]', ".location"):
                el = await card_el.query_selector(sel)
                if el:
                    raw["location"] = (await el.inner_text()).strip()
                    break

            # time_ago
            for sel in ('[data-testid="time-ago"]', ".time-ago", "time"):
                el = await card_el.query_selector(sel)
                if el:
                    raw["time_ago"] = (await el.inner_text()).strip()
                    break

            # link
            link_el = await card_el.query_selector("a.article-link, a[href*='/de/a/']")
            if link_el:
                href = (await link_el.get_attribute("href")) or ""
                raw["link"] = (
                    href if href.startswith("http")
                    else f"https://www.ricardo.ch{href}"
                )

            # image
            img_el = await card_el.query_selector("img")
            if img_el:
                src = (
                    await img_el.get_attribute("src")
                    or await img_el.get_attribute("data-src")
                    or ""
                )
                if src:
                    raw["image_url"] = src

            cards.append(raw)
        except Exception as exc:
            logger.debug("Error extracting single card: {}", exc)
            continue

    return cards


# ─── Step 4: parse_full_ad ───────────────────────────────────────────────────

async def parse_full_ad(page: Page, ad_url: str) -> dict:
    """Fetch a full listing page and extract extended fields (images, description, etc.)."""
    result: dict = {"url": ad_url}
    await safe_goto(page, ad_url)
    try:
        await page.wait_for_selector('[data-testid="article-detail"]', timeout=10_000)
    except Exception:
        pass  # Continue and extract whatever is available

    # Description
    try:
        el = await page.query_selector('[data-testid="description"]')
        if el:
            result["description"] = (await el.inner_text()).strip()
    except Exception:
        pass

    # Gallery images
    try:
        imgs = await page.query_selector_all('img[data-testid="gallery-image"]')
        srcs = [await img.get_attribute("src") for img in imgs]
        result["image_urls"] = [s for s in srcs if s]
        if not result["image_urls"]:
            all_imgs = await page.query_selector_all("img")
            urls = []
            for img in all_imgs:
                src = await img.get_attribute("src") or ""
                if src and re.search(r"cdn|media|img|photo", src, re.I):
                    urls.append(src)
            result["image_urls"] = urls
    except Exception:
        result["image_urls"] = []

    # Views count
    try:
        el = await page.query_selector('[data-testid="views-count"], .views-count')
        if el:
            m = re.search(r"\d+", await el.inner_text())
            if m:
                result["views_count"] = int(m.group())
    except Exception:
        pass

    # Bids / purchases count
    try:
        el = await page.query_selector('[data-testid="bids-count"], .bids-count')
        if el:
            m = re.search(r"\d+", await el.inner_text())
            if m:
                result["bids_count"] = int(m.group())
    except Exception:
        pass

    # Exact publication date
    try:
        el = await page.query_selector('[data-testid="creation-date"], time')
        if el:
            dt_attr = await el.get_attribute("datetime") or ""
            dt_text = (await el.inner_text()).strip()
            result["creation_date"] = _try_parse_date(dt_attr or dt_text)
    except Exception:
        pass

    return result


# ─── Step 5: parse_seller_profile ────────────────────────────────────────────

async def parse_seller_profile(page: Page, username: str) -> dict:
    """Fetch the seller profile at /de/u/{username} and extract registration/sales data."""
    url = f"https://www.ricardo.ch/de/u/{username}"
    result: dict = {"username": username}

    success = await safe_goto(page, url)
    if not success:
        return result

    try:
        await page.wait_for_selector(
            '[data-testid="user-profile"], .user-profile, .profile-header',
            timeout=8_000,
        )
    except Exception:
        pass

    try:
        page_text = await page.inner_text("body")
    except Exception:
        return result

    # registration_date: "Mitglied seit März 2022"
    m = re.search(
        r"Mitglied\s+seit\s+([\w.]+\.?\s*\d{4}|\d{2}\.\d{2}\.\d{4}|\d{4})",
        page_text, re.I,
    )
    if m:
        raw = m.group(1).strip()
        year_m = re.search(r"\d{4}", raw)
        if year_m:
            year = int(year_m.group())
            month_raw = re.sub(r"[\d.\s]", "", raw).strip().lower().rstrip(".")
            month = _DE_MONTHS.get(month_raw) or _DE_MONTHS.get(month_raw[:3], 1)
            try:
                result["registration_date"] = datetime(year, month, 1, tzinfo=timezone.utc)
            except ValueError:
                result["registration_date"] = datetime(year, 1, 1, tzinfo=timezone.utc)

    # sales_count: "142 Verkäufe"
    m = re.search(r"(\d[\d'.]*)\s+Verk[äa]ufe", page_text, re.I)
    if m:
        try:
            result["sales_count"] = int(re.sub(r"['\s.]", "", m.group(1)))
        except ValueError:
            pass

    # positive_feedback: "98 % positiv"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*positiv", page_text, re.I)
    if m:
        try:
            result["positive_feedback"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    return result


# ─── Step 8: scrape_new_ads ───────────────────────────────────────────────────

async def scrape_new_ads(filters: dict, seen_ids: set) -> list["Listing"]:
    """Main scraping entry point using Playwright.

    Builds a search URL, loads the page, extracts article cards, and returns
    new Listing objects for IDs not in *seen_ids*.

    Cloudflare challenges trigger a 5-minute sleep and full browser restart.
    Other errors are counted; after MAX_CONSECUTIVE_ERRORS the browser restarts.
    """
    global _consecutive_errors

    ctx = await _ensure_browser()
    page = await ctx.new_page()
    try:
        url = build_search_url(filters)
        logger.info("🔍 Поиск: {}", url)

        try:
            await load_search_page(page, url)
        except CloudflareBlockError as exc:
            logger.warning("🛡️ Cloudflare block: {} — пауза 5 мин + рестарт браузера", exc)
            _consecutive_errors = 0
            await page.close()
            await init_browser()
            await asyncio.sleep(300)  # 5-minute mandatory pause
            return []
        except RuntimeError as exc:
            logger.error("load_search_page: {}", exc)
            _consecutive_errors += 1
            if _consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.warning("≥%d ошибок подряд — перезапускаем браузер", MAX_CONSECUTIVE_ERRORS)
                await init_browser()
                _consecutive_errors = 0
            return []

        cards = await extract_cards(page)
        logger.info("📋 Карточек на странице: {}", len(cards))

    except CloudflareBlockError as exc:
        logger.warning("🛡️ Cloudflare block (outer): {} — пауза 5 мин", exc)
        _consecutive_errors = 0
        await init_browser()
        await asyncio.sleep(300)
        return []
    except Exception as exc:
        logger.error("scrape_new_ads error: {}", exc)
        _consecutive_errors += 1
        if _consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            logger.warning("≥%d ошибок подряд — перезапускаем браузер", MAX_CONSECUTIVE_ERRORS)
            await init_browser()
            _consecutive_errors = 0
        return []
    finally:
        try:
            await page.close()
        except Exception:
            pass

    new_listings: list[Listing] = []
    for card in cards:
        article_id = card.get("article_id", "")
        if not article_id or article_id in seen_ids:
            continue
        if not card.get("title"):
            continue
        link = card.get("link") or f"https://www.ricardo.ch/de/a/{article_id}/"
        seller_name = card.get("seller_username") or ""
        listing = Listing(
            listing_id=article_id,
            title=card["title"],
            price=card.get("price"),
            url=link,
            image_url=card.get("image_url") or "",
            seller_name=seller_name,
            seller_url=(
                f"https://www.ricardo.ch/de/shop/{seller_name}/ratings/"
                if seller_name else ""
            ),
            location=card.get("location") or "",
        )
        new_listings.append(listing)

    _consecutive_errors = 0
    logger.info("✅ Новых объявлений: {}", len(new_listings))
    return new_listings


# ─── Seller enrichment (aiohttp, lightweight) ────────────────────────────────

async def fetch_seller_info(
    session: aiohttp.ClientSession,
    seller_url: str,
) -> tuple[Optional[datetime], Optional[int], Optional[int]]:
    """Fetch seller registration date, sold count, and purchases count via aiohttp."""
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
        logger.debug("fetch_seller_info error ({}): {}", ratings_url, exc)
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
                        mx = re.search(r"\b(20\d{2}|19\d{2})\b", str(raw))
                        if mx:
                            reg_date = datetime(int(mx.group(1)), 1, 1, tzinfo=timezone.utc)
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
        mx = re.search(r"Mitglied\s+seit\s+(\d{2}\.\d{2}\.(\d{4})|\d{4})", page_text, re.I)
        if mx:
            full = mx.group(1)
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
        mx = re.search(r"seit\s+(20\d{2}|19\d{2})\b", page_text, re.I)
        if mx:
            reg_date = datetime(int(mx.group(1)), 1, 1, tzinfo=timezone.utc)

    if sold_count is None:
        mx = re.search(r"als?\s+Verk[äa]ufer[^\d]*(\d[\d'.\s]*)", page_text, re.I)
        if mx:
            try:
                sold_count = int(re.sub(r"['\s.]", "", mx.group(1)))
            except ValueError:
                pass

    if purchases_count is None:
        mx = re.search(r"als?\s+K[äa]ufer[^\d]*(\d[\d'.\s]*)", page_text, re.I)
        if mx:
            try:
                purchases_count = int(re.sub(r"['\s.]", "", mx.group(1)))
            except ValueError:
                pass

    return reg_date, sold_count, purchases_count


async def enrich_seller_info(
    session: aiohttp.ClientSession,
    listings: list["Listing"],
) -> None:
    """Enrich listings with seller registration date and sales count (aiohttp)."""
    tasks = [_enrich_one(session, lst) for lst in listings if lst.seller_url]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _enrich_one(session: aiohttp.ClientSession, listing: "Listing") -> None:
    reg, sold, purchases = await fetch_seller_info(session, listing.seller_url)
    listing.seller_registered = reg
    listing.sold_count = sold
    listing.purchases_count = purchases


# ─── Main entry point (backward-compat wrapper) ───────────────────────────────

async def probe_or_search(
    session: aiohttp.ClientSession,
    user_filters: dict,
    n: int = 50,
) -> list["Listing"]:
    """Search for listings using Playwright and return up to *n* results.

    The *session* parameter is kept for API compatibility with bot.py;
    it is passed to ``enrich_seller_info`` for fast seller data fetching.
    """
    results = await scrape_new_ads(user_filters, seen_ids=set())

    # Deduplicate by listing_id
    seen: set[str] = set()
    deduped: list[Listing] = []
    for lst in results:
        if lst.listing_id not in seen:
            seen.add(lst.listing_id)
            deduped.append(lst)
    return deduped[:n]

