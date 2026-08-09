import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def check_amazon_stock(url):
    """
    Scrapes the Amazon URL to check if the product is in stock and retrieves details.
    Returns a dictionary with status, title, price, etc.
    """
    ua = UserAgent()
    headers = {
        'User-Agent': ua.random,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return {"status": "error", "message": str(e)}

    soup = BeautifulSoup(response.content, 'html.parser')

    # Amazon frequently changes its DOM structure, so we look for common IDs used for stock status
    
    # 1. Check title
    title_element = soup.find(id="productTitle")
    title = title_element.text.strip() if title_element else "Unknown Product"

    # 2. Check stock status
    in_stock = False
    availability_div = soup.find(id="availability")
    
    if availability_div:
        availability_text = availability_div.text.lower()
        if "currently unavailable" in availability_text or "out of stock" in availability_text or "we don't know when or if this item will be back in stock" in availability_text:
            in_stock = False
        else:
            # Usually says "In Stock" or "Only X left in stock"
            in_stock = True
    else:
        # Sometimes the 'add to cart' button is a good indicator
        add_to_cart_btn = soup.find(id="add-to-cart-button")
        buy_now_btn = soup.find(id="buy-now-button")
        if add_to_cart_btn or buy_now_btn:
            in_stock = True

    # 3. Try to get price
    price = "Price not found"
    price_element = soup.find("span", class_="a-price-whole")
    fraction_element = soup.find("span", class_="a-price-fraction")
    symbol_element = soup.find("span", class_="a-price-symbol")
    
    if price_element:
        symbol = symbol_element.text if symbol_element else "$"
        fraction = fraction_element.text if fraction_element else "00"
        price = f"{symbol}{price_element.text}{fraction}"

    return {
        "status": "success",
        "in_stock": in_stock,
        "title": title,
        "price": price,
        "url": url
    }

if __name__ == "__main__":
    # Test script locally
    test_url = "https://www.amazon.com/dp/B0815XFSGK" # Example Amazon URL
    print("Testing scraper...")
    result = check_amazon_stock(test_url)
    print(result)
