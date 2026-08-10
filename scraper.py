import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import asyncio
import random
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Module-level setup (initialized once) ────────────────────────────────────

# Build a rotating user-agent pool once at import time
try:
    _ua = UserAgent(fallback="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    _USER_AGENTS = [_ua.random for _ in range(20)]
except Exception:
    _USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    ]

# ─── Proxy Configuration (Rotating Pool) ─────────────────────────────────────
# Loads proxies from a text file in ip:port:username:password format (Webshare export).
# Each request picks a random proxy for maximum anti-detection.
# Falls back to PROXY_URL env var if no file found, or direct if nothing is set.

PROXY_FILE = os.getenv("PROXY_FILE", "Webshare 10 proxies.txt")

def _load_proxy_pool():
    """Load proxies from a Webshare-format text file (ip:port:user:pass per line)."""
    pool = []

    # Method 1: Load from proxy file
    if os.path.exists(PROXY_FILE):
        try:
            with open(PROXY_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(':')
                    if len(parts) == 4:
                        ip, port, user, password = parts
                        proxy_url = f"http://{user}:{password}@{ip}:{port}"
                        pool.append(proxy_url)
                    elif len(parts) == 2:
                        # ip:port without auth
                        ip, port = parts
                        proxy_url = f"http://{ip}:{port}"
                        pool.append(proxy_url)
            if pool:
                logger.info(f"🌐 Loaded {len(pool)} proxies from {PROXY_FILE}")
                return pool
        except Exception as e:
            logger.error(f"Failed to load proxy file: {e}")

    # Method 2: Single PROXY_URL from .env
    single_proxy = os.getenv("PROXY_URL", "").strip()
    if single_proxy:
        logger.info(f"🌐 Using single proxy from PROXY_URL")
        return [single_proxy]

    logger.warning("⚠️ No proxies configured — requests go direct. Will get blocked on Railway!")
    return []

_PROXY_POOL = _load_proxy_pool()


def _get_random_proxy():
    """Pick a random proxy from the pool. Returns a proxies dict or None."""
    if not _PROXY_POOL:
        return None
    proxy_url = random.choice(_PROXY_POOL)
    return {"http": proxy_url, "https": proxy_url}


# Persistent session with connection pooling and automatic retries
# NOTE: We do NOT set proxies on the session — we rotate per-request instead.
_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=1,           # 1s, 2s, 4s between retries
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_adapter = HTTPAdapter(
    max_retries=_retry_strategy,
    pool_connections=10,
    pool_maxsize=20,
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


# ─── CAPTCHA / Block Detection ───────────────────────────────────────────────

CAPTCHA_MARKERS = [
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "api-services-support@amazon",
    "/captcha/",
]


def _is_blocked(soup, response_text):
    """Detect if Amazon returned a CAPTCHA or bot-detection page."""
    text_lower = response_text.lower()
    for marker in CAPTCHA_MARKERS:
        if marker in text_lower:
            return True
    # Also check if the page is suspiciously small (captcha pages are tiny)
    if len(response_text) < 5000 and "robot" in text_lower:
        return True
    return False


# ─── Stock Detection ─────────────────────────────────────────────────────────

# Signals that the product is OUT of stock
OUT_OF_STOCK_PHRASES = [
    "currently unavailable",
    "out of stock",
    "we don't know when or if this item will be back in stock",
    "not available",
    "unavailable",
    "no offers found",
    "undeliverable to this address",
]

# Signals that the product IS in stock
IN_STOCK_PHRASES = [
    "in stock",
    "left in stock",
    "in stock soon",
    "available from these sellers",
    "add to cart",
    "buy now",
]


def _detect_stock_status(soup):
    """
    Comprehensive stock detection using multiple Amazon DOM patterns.
    Returns True (in stock), False (out of stock), or None (unknown).
    """
    # ── Method 1: Direct out-of-stock indicator ──
    out_of_stock_div = soup.find(id="outOfStock")
    if out_of_stock_div:
        return False

    # ── Method 2: #availability div (most common) ──
    availability_div = soup.find(id="availability")
    if availability_div:
        avail_text = availability_div.get_text(separator=" ").lower().strip()
        # Check OOS phrases first (more specific)
        for phrase in OUT_OF_STOCK_PHRASES:
            if phrase in avail_text:
                return False
        # Check in-stock phrases
        for phrase in IN_STOCK_PHRASES:
            if phrase in avail_text:
                return True

    # ── Method 3: Add-to-cart / Buy-now buttons ──
    add_to_cart = (
        soup.find(id="add-to-cart-button")
        or soup.find(id="add-to-cart-button-ubb")
        or soup.find("input", {"id": "add-to-cart-button"})
        or soup.find("input", {"name": "submit.add-to-cart"})
        or soup.find("span", {"id": "submit.add-to-cart"})
    )
    buy_now = (
        soup.find(id="buy-now-button")
        or soup.find(id="one-click-button")
    )
    if add_to_cart or buy_now:
        return True

    # ── Method 4: Deal badge (item on deal → in stock) ──
    deal_badge = soup.find(id="dealBadge") or soup.find("span", {"class": "dealBadge"})
    if deal_badge:
        return True

    # ── Method 5: Price presence as a weak signal ──
    price_whole = soup.find("span", class_="a-price-whole")
    price_block = soup.find(id="priceblock_ourprice") or soup.find(id="priceblock_dealprice")
    core_price = soup.find("span", class_="a-price")
    if price_whole or price_block or core_price:
        # Has a price displayed — likely in stock, but not definitive
        # Only use this if we haven't found any OOS signals
        oos_div = soup.find(id="availability")
        if oos_div:
            oos_text = oos_div.get_text(separator=" ").lower()
            for phrase in OUT_OF_STOCK_PHRASES:
                if phrase in oos_text:
                    return False
        return True

    # ── Method 6: Check entire page text as last resort ──
    page_text = soup.get_text(separator=" ").lower()
    for phrase in OUT_OF_STOCK_PHRASES:
        if phrase in page_text:
            return False

    # If we found a product title but none of the above, assume OOS to be safe
    if soup.find(id="productTitle"):
        return False

    return None  # Can't determine


def _extract_price(soup):
    """Extract price from multiple possible Amazon DOM locations."""
    # Method 1: Standard price display
    price_whole = soup.find("span", class_="a-price-whole")
    price_fraction = soup.find("span", class_="a-price-fraction")
    price_symbol = soup.find("span", class_="a-price-symbol")

    if price_whole:
        symbol = price_symbol.text.strip() if price_symbol else "₹"
        whole = price_whole.text.strip().rstrip(".")
        fraction = price_fraction.text.strip() if price_fraction else "00"
        return f"{symbol}{whole}.{fraction}"

    # Method 2: Price block IDs
    for pid in ["priceblock_ourprice", "priceblock_dealprice", "priceblock_saleprice"]:
        el = soup.find(id=pid)
        if el:
            return el.text.strip()

    # Method 3: Core price span
    core = soup.find("span", class_="a-price")
    if core:
        offscreen = core.find("span", class_="a-offscreen")
        if offscreen:
            return offscreen.text.strip()

    # Method 4: Deal price
    deal_price = soup.find("span", id="dealprice_feature_div")
    if deal_price:
        price_span = deal_price.find("span", class_="a-offscreen")
        if price_span:
            return price_span.text.strip()

    return "Price not found"


def _extract_title(soup):
    """Extract product title."""
    title_el = soup.find(id="productTitle")
    if title_el:
        return title_el.text.strip()
    # Fallback: meta title
    meta_title = soup.find("meta", {"name": "title"})
    if meta_title and meta_title.get("content"):
        return meta_title["content"].strip()
    return "Unknown Product"


# ─── Main Public API ─────────────────────────────────────────────────────────

def check_amazon_stock(url):
    """
    Scrapes the Amazon URL to check stock status.
    Returns dict with keys: status, in_stock, title, price, url
    status can be: "success", "error", "blocked"
    """
    headers = {
        'User-Agent': random.choice(_USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'Sec-Ch-Ua': '"Chromium";v="125", "Not.A/Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
    }

    # Pick a random proxy for this request (rotating IP)
    proxy = _get_random_proxy()

    try:
        response = _session.get(url, headers=headers, timeout=(5, 10), proxies=proxy)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return {"status": "error", "message": str(e), "url": url}

    # Use lxml for faster parsing if available, fall back to html.parser
    try:
        soup = BeautifulSoup(response.content, 'lxml')
    except Exception:
        soup = BeautifulSoup(response.content, 'html.parser')

    # Check for CAPTCHA / bot detection
    if _is_blocked(soup, response.text):
        logger.warning(f"🛡️ Amazon blocked/CAPTCHA detected for {url}")
        return {"status": "blocked", "message": "Amazon CAPTCHA/bot detection triggered", "url": url}

    title = _extract_title(soup)
    price = _extract_price(soup)
    in_stock = _detect_stock_status(soup)

    # If we couldn't determine stock at all, treat as error
    if in_stock is None:
        logger.warning(f"⚠️ Could not determine stock status for {url}")
        return {
            "status": "error",
            "message": "Could not determine stock status — page layout unrecognized",
            "url": url,
        }

    return {
        "status": "success",
        "in_stock": in_stock,
        "title": title,
        "price": price,
        "url": url,
    }


async def check_amazon_stock_async(url):
    """Async wrapper — runs the blocking HTTP call in a thread pool."""
    return await asyncio.to_thread(check_amazon_stock, url)


if __name__ == "__main__":
    # Test script locally
    test_url = "https://www.amazon.in/dp/B0815XFSGK"
    print("Testing scraper...")
    result = check_amazon_stock(test_url)
    print(result)
