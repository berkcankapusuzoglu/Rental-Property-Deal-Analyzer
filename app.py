import os, json, re, webbrowser, threading, time, asyncio
from pathlib import Path
from urllib.parse import urlparse
from collections import defaultdict
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from json import JSONDecodeError
from starlette.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
import uvicorn

load_dotenv()
app = FastAPI()


async def _parse_json_object(request: Request) -> tuple[dict, JSONResponse | None]:
    """Parse request JSON and require a top-level object."""
    try:
        body = await request.json()
    except JSONDecodeError:
        return {}, JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)

    if not isinstance(body, dict):
        return {}, JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)

    return body, None


# ---------------------------------------------------------------------------
# Rate Limiter (in-memory, per-IP)
# ---------------------------------------------------------------------------
_rate_limits: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str, limit: int, window: int = 60) -> bool:
    """Return True if the request is within rate limits."""
    now = time.time()
    timestamps = _rate_limits[ip]
    # Prune old entries
    _rate_limits[ip] = [t for t in timestamps if now - t < window]
    if len(_rate_limits[ip]) >= limit:
        return False
    _rate_limits[ip].append(now)
    return True

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_get(obj, *keys, default=None):
    """Safely traverse nested dicts/lists."""
    current = obj
    for key in keys:
        try:
            if isinstance(current, dict):
                current = current[key]
            elif isinstance(current, (list, tuple)):
                current = current[int(key)]
            else:
                return default
        except (KeyError, IndexError, TypeError, ValueError):
            return default
    return current


def _format_address(addr_obj):
    """Build a single-line address from Zillow address dict."""
    if not addr_obj or not isinstance(addr_obj, dict):
        return None
    parts = [
        addr_obj.get("streetAddress", ""),
        addr_obj.get("city", ""),
    ]
    state = addr_obj.get("state", "")
    zipcode = addr_obj.get("zipcode", "")
    state_zip = f"{state} {zipcode}".strip()
    line = ", ".join(p for p in parts if p)
    if state_zip:
        line = f"{line}, {state_zip}" if line else state_zip
    return line or None


def _extract_tax_history(raw_history):
    """Normalise Zillow taxHistory array."""
    if not raw_history or not isinstance(raw_history, list):
        return []
    result = []
    for entry in raw_history:
        if not isinstance(entry, dict):
            continue
        year = entry.get("time") or entry.get("year")
        amount = entry.get("taxPaid") or entry.get("amount")
        # 'time' is sometimes an epoch-ms; convert to year
        if isinstance(year, (int, float)) and year > 3000:
            from datetime import datetime, timezone
            try:
                year = datetime.fromtimestamp(year / 1000, tz=timezone.utc).year
            except Exception:
                pass
        if year is not None:
            result.append({"year": int(year) if year else None, "amount": amount})
    return result


def _get_image_url(prop):
    """Extract a representative image URL."""
    url = prop.get("hiResImageLink")
    if url:
        return url
    photos = prop.get("responsivePhotos") or prop.get("photos") or []
    if photos and isinstance(photos, list):
        first = photos[0]
        if isinstance(first, dict):
            # Try multiple known sub-paths
            for subkey in ("mixedSources", "sources"):
                sources = first.get(subkey)
                if sources and isinstance(sources, dict):
                    for quality in ("jpeg", "webp", "png"):
                        imgs = sources.get(quality)
                        if imgs and isinstance(imgs, list):
                            # pick the largest
                            best = max(imgs, key=lambda x: x.get("width", 0) if isinstance(x, dict) else 0)
                            if isinstance(best, dict) and best.get("url"):
                                return best["url"]
            # Direct url on photo object
            if first.get("url"):
                return first["url"]
    return None


def _build_result(prop):
    """Build the flat result dict from a Zillow property dict."""
    tax_history = _extract_tax_history(prop.get("taxHistory"))
    annual_tax = None
    if tax_history:
        annual_tax = tax_history[0].get("amount")

    lot_size = prop.get("lotSize") or prop.get("lotAreaValue")
    # lotSize sometimes comes as a string like "6,000 sqft"
    if isinstance(lot_size, str):
        nums = re.findall(r"[\d,]+", lot_size)
        if nums:
            try:
                lot_size = int(nums[0].replace(",", ""))
            except ValueError:
                lot_size = None

    return {
        "address": _format_address(prop.get("address")),
        "price": prop.get("price") or prop.get("listPrice"),
        "beds": prop.get("bedrooms"),
        "baths": prop.get("bathrooms"),
        "sqft": prop.get("livingArea"),
        "lotSize": lot_size,
        "yearBuilt": prop.get("yearBuilt"),
        "propertyType": prop.get("homeType"),
        "zestimate": prop.get("zestimate"),
        "rentZestimate": prop.get("rentZestimate"),
        "taxHistory": tax_history,
        "annualTax": annual_tax,
        "hoaFee": prop.get("monthlyHoaFee") or 0,
        "description": prop.get("description"),
        "imageUrl": _get_image_url(prop),
    }


# ---------------------------------------------------------------------------
# Extraction strategies
# ---------------------------------------------------------------------------

def _extract_from_next_data(soup):
    """Primary: parse __NEXT_DATA__ -> gdpClientCache / apiCache."""
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return None

    try:
        next_data = json.loads(script_tag.string)
    except (json.JSONDecodeError, TypeError):
        return None

    # Strategy A: gdpClientCache (most common)
    gdp_cache = _safe_get(next_data, "props", "pageProps", "gdpClientCache")
    if gdp_cache and isinstance(gdp_cache, (dict, str)):
        # gdpClientCache may itself be a JSON string
        if isinstance(gdp_cache, str):
            try:
                gdp_cache = json.loads(gdp_cache)
            except json.JSONDecodeError:
                gdp_cache = {}

        if isinstance(gdp_cache, dict):
            for _key, value in gdp_cache.items():
                # Each value is often a stringified JSON blob
                parsed = value
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        continue

                # Look for property data
                prop = None
                if isinstance(parsed, dict):
                    prop = parsed.get("property")
                    if not prop:
                        # Sometimes nested under data -> property
                        prop = _safe_get(parsed, "data", "property")
                if prop and isinstance(prop, dict):
                    return _build_result(prop)

    # Strategy B: apiCache
    api_cache = _safe_get(next_data, "props", "pageProps", "apiCache")
    if api_cache and isinstance(api_cache, (dict, str)):
        if isinstance(api_cache, str):
            try:
                api_cache = json.loads(api_cache)
            except json.JSONDecodeError:
                api_cache = {}

        if isinstance(api_cache, dict):
            for _key, value in api_cache.items():
                parsed = value
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                if isinstance(parsed, dict):
                    prop = parsed.get("property")
                    if not prop:
                        prop = _safe_get(parsed, "data", "property")
                    if prop and isinstance(prop, dict):
                        return _build_result(prop)

    # Strategy C: direct pageProps.property (newer layouts)
    prop = _safe_get(next_data, "props", "pageProps", "property")
    if prop and isinstance(prop, dict) and (prop.get("address") or prop.get("price")):
        return _build_result(prop)

    # Strategy D: componentProps (may contain its own gdpClientCache)
    comp_props = _safe_get(next_data, "props", "pageProps", "componentProps")
    if comp_props and isinstance(comp_props, dict):
        # D1: direct property on componentProps values
        for _key, value in comp_props.items():
            if isinstance(value, dict):
                prop = value.get("property")
                if prop and isinstance(prop, dict):
                    return _build_result(prop)

        # D2: gdpClientCache nested inside componentProps
        gdp_nested = comp_props.get("gdpClientCache")
        if gdp_nested:
            if isinstance(gdp_nested, str):
                try:
                    gdp_nested = json.loads(gdp_nested)
                except json.JSONDecodeError:
                    gdp_nested = {}
            if isinstance(gdp_nested, dict):
                for _key, value in gdp_nested.items():
                    parsed = value
                    if isinstance(value, str):
                        try:
                            parsed = json.loads(value)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(parsed, dict):
                        prop = parsed.get("property")
                        if not prop:
                            prop = _safe_get(parsed, "data", "property")
                        if prop and isinstance(prop, dict):
                            return _build_result(prop)

    return None


def _extract_from_ld_json(soup):
    """Fallback: parse application/ld+json structured data."""
    ld_scripts = soup.find_all("script", type="application/ld+json")
    for tag in ld_scripts:
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue

        # Can be a list or single object
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if item_type in ("SingleFamilyResidence", "Residence", "Product", "House", "Apartment"):
                # ld+json has a different shape; map what we can
                address_obj = item.get("address", {})
                if isinstance(address_obj, dict):
                    addr = {
                        "streetAddress": address_obj.get("streetAddress", ""),
                        "city": address_obj.get("addressLocality", ""),
                        "state": address_obj.get("addressRegion", ""),
                        "zipcode": address_obj.get("postalCode", ""),
                    }
                else:
                    addr = None

                floor_size = item.get("floorSize", {})
                sqft = None
                if isinstance(floor_size, dict):
                    sqft = floor_size.get("value")
                elif isinstance(floor_size, (int, float)):
                    sqft = floor_size

                price = None
                offers = item.get("offers", {})
                if isinstance(offers, dict):
                    price = offers.get("price")
                if not price:
                    price = item.get("price")

                return {
                    "address": _format_address(addr) if addr else item.get("name"),
                    "price": price,
                    "beds": item.get("numberOfRooms") or item.get("bedrooms"),
                    "baths": item.get("bathrooms"),
                    "sqft": sqft,
                    "lotSize": None,
                    "yearBuilt": item.get("yearBuilt"),
                    "propertyType": item_type,
                    "zestimate": None,
                    "rentZestimate": None,
                    "taxHistory": [],
                    "annualTax": None,
                    "hoaFee": 0,
                    "description": item.get("description"),
                    "imageUrl": item.get("image"),
                }
    return None


def _extract_from_dom(soup):
    """Fallback: extract property data from rendered DOM elements and meta tags."""
    result = {
        "address": None, "price": None, "beds": None, "baths": None,
        "sqft": None, "lotSize": None, "yearBuilt": None, "propertyType": None,
        "zestimate": None, "rentZestimate": None, "taxHistory": [],
        "annualTax": None, "hoaFee": 0, "description": None, "imageUrl": None,
    }

    # Try og:title for address
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        result["address"] = og_title["content"].split("|")[0].strip()

    # Try og:image
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        result["imageUrl"] = og_image["content"]

    # Try meta description for details
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc["content"]
        result["description"] = desc

        # Parse common patterns like "$350,000 - 3 bed, 2 bath, 1,500 sqft"
        price_m = re.search(r"\$[\d,]+", desc)
        if price_m:
            try:
                result["price"] = int(price_m.group().replace("$", "").replace(",", ""))
            except ValueError:
                pass

        beds_m = re.search(r"(\d+)\s*(?:bed|br)", desc, re.IGNORECASE)
        if beds_m:
            result["beds"] = int(beds_m.group(1))

        baths_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:bath|ba)", desc, re.IGNORECASE)
        if baths_m:
            result["baths"] = float(baths_m.group(1))

        sqft_m = re.search(r"([\d,]+)\s*(?:sq\s*ft|sqft)", desc, re.IGNORECASE)
        if sqft_m:
            try:
                result["sqft"] = int(sqft_m.group(1).replace(",", ""))
            except ValueError:
                pass

    # Search for JSON-like data blobs in script tags (Zillow often embeds property
    # data in various script tags beyond __NEXT_DATA__)
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 100:
            continue

        # Look for common Zillow data patterns
        for pattern in [r'"price"\s*:\s*(\d+)', r'"listPrice"\s*:\s*(\d+)']:
            m = re.search(pattern, text)
            if m and not result["price"]:
                try:
                    result["price"] = int(m.group(1))
                except ValueError:
                    pass

        if not result["beds"]:
            m = re.search(r'"bedrooms"\s*:\s*(\d+)', text)
            if m:
                result["beds"] = int(m.group(1))

        if not result["baths"]:
            m = re.search(r'"bathrooms"\s*:\s*([\d.]+)', text)
            if m:
                result["baths"] = float(m.group(1))

        if not result["sqft"]:
            m = re.search(r'"livingArea"\s*:\s*(\d+)', text)
            if m:
                result["sqft"] = int(m.group(1))

        if not result["yearBuilt"]:
            m = re.search(r'"yearBuilt"\s*:\s*(\d{4})', text)
            if m:
                result["yearBuilt"] = int(m.group(1))

        if not result["zestimate"]:
            m = re.search(r'"zestimate"\s*:\s*(\d+)', text)
            if m:
                result["zestimate"] = int(m.group(1))

        if not result["rentZestimate"]:
            m = re.search(r'"rentZestimate"\s*:\s*(\d+)', text)
            if m:
                result["rentZestimate"] = int(m.group(1))

    # Only return if we found at least an address or price
    if result["address"] or result["price"]:
        return result

    return None


# ---------------------------------------------------------------------------
# Playwright fallback fetcher
# ---------------------------------------------------------------------------

async def _fetch_with_playwright(url: str) -> str:
    """Use a headless browser to fetch the page (bypasses bot detection)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()

        # Remove webdriver flag to avoid bot detection
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait for JS to populate data (Zillow is heavily JS-rendered)
        await page.wait_for_timeout(3000)

        # Try scrolling to trigger lazy-loaded content
        await page.evaluate("window.scrollBy(0, 300)")
        await page.wait_for_timeout(1000)

        html = await page.content()
        await browser.close()
    return html


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

IS_CLOUD = bool(os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT"))


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html = Path("index.html").read_text(encoding="utf-8")
    if IS_CLOUD:
        # Inject flag so frontend can disable scraping-dependent features
        html = html.replace("</head>", '<script>window.__CLOUD_DEMO__=true;</script></head>')
    return html


def _detect_source(hostname: str) -> str:
    """Detect data source from URL hostname."""
    if hostname and hostname.endswith("redfin.com"):
        return "redfin"
    if hostname and hostname.endswith("zillow.com"):
        return "zillow"
    return "unknown"


def _extract_redfin(soup) -> dict | None:
    """Extract property data from a Redfin listing page."""
    result = {
        "address": None, "price": None, "beds": None, "baths": None,
        "sqft": None, "lotSize": None, "yearBuilt": None, "propertyType": None,
        "zestimate": None, "rentZestimate": None, "taxHistory": [],
        "annualTax": None, "hoaFee": 0, "description": None, "imageUrl": None,
    }

    # Helper to extract address from a schema.org object
    def _extract_address(obj: dict) -> str | None:
        addr_obj = obj.get("address", {})
        if isinstance(addr_obj, dict):
            parts = [addr_obj.get("streetAddress", ""),
                     addr_obj.get("addressLocality", "")]
            state = addr_obj.get("addressRegion", "")
            zipcode = addr_obj.get("postalCode", "")
            addr = ", ".join(p for p in parts if p)
            if state:
                addr += f", {state} {zipcode}".rstrip()
            return addr if addr else None
        elif isinstance(addr_obj, str):
            return addr_obj
        return None

    # Helper to check if @type matches any known residential/listing type
    def _type_matches(item_type, targets) -> bool:
        if isinstance(item_type, list):
            return any(t in targets for t in item_type)
        return item_type in targets

    LISTING_TYPES = {"SingleFamilyResidence", "Residence", "Product",
                     "House", "Apartment", "RealEstateListing"}
    RESIDENTIAL_TYPES = {"SingleFamilyResidence", "Residence", "House",
                         "Apartment", "Condominium", "TownHouse"}

    # 1) ld+json (Redfin usually has good structured data)
    ld_scripts = soup.find_all("script", type="application/ld+json")
    for tag in ld_scripts:
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if not _type_matches(item_type, LISTING_TYPES):
                continue

            # Extract top-level data (address, image, description, price)
            if not result["address"]:
                result["address"] = _extract_address(item)
            result["description"] = result["description"] or item.get("description")
            img = item.get("image") or item.get("photo")
            if isinstance(img, list) and img:
                img = img[0]
            if isinstance(img, dict):
                img = img.get("contentUrl") or img.get("url")
            if not result["imageUrl"] and isinstance(img, str):
                result["imageUrl"] = img

            # Price from offers or top-level
            offers = item.get("offers", {})
            if isinstance(offers, dict) and not result["price"]:
                result["price"] = offers.get("price")
            if not result["price"]:
                result["price"] = item.get("price")

            # Direct property fields (if at top level)
            result["beds"] = result["beds"] or item.get("numberOfRooms") or item.get("numberOfBedrooms")
            result["baths"] = result["baths"] or item.get("numberOfBathroomsTotal") or item.get("numberOfFullBathrooms")
            result["yearBuilt"] = result["yearBuilt"] or item.get("yearBuilt")
            floor_size = item.get("floorSize", {})
            if not result["sqft"]:
                if isinstance(floor_size, dict):
                    result["sqft"] = floor_size.get("value")
                elif isinstance(floor_size, (int, float)):
                    result["sqft"] = int(floor_size)

            # Traverse mainEntity for nested residential data (Redfin pattern)
            main_entity = item.get("mainEntity", {})
            if isinstance(main_entity, dict):
                me_type = main_entity.get("@type", "")
                if _type_matches(me_type, RESIDENTIAL_TYPES) or main_entity.get("numberOfBedrooms"):
                    if not result["address"]:
                        result["address"] = _extract_address(main_entity)
                    result["beds"] = result["beds"] or main_entity.get("numberOfBedrooms") or main_entity.get("numberOfRooms")
                    result["baths"] = result["baths"] or main_entity.get("numberOfBathroomsTotal") or main_entity.get("numberOfFullBathrooms")
                    result["yearBuilt"] = result["yearBuilt"] or main_entity.get("yearBuilt")
                    me_floor = main_entity.get("floorSize", {})
                    if not result["sqft"]:
                        if isinstance(me_floor, dict):
                            result["sqft"] = me_floor.get("value")
                        elif isinstance(me_floor, (int, float)):
                            result["sqft"] = int(me_floor)

    # 2) Fallback: parse from meta tags
    if not result["address"]:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            result["address"] = og_title["content"].split("|")[0].strip()

    if not result["imageUrl"]:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            result["imageUrl"] = og_img["content"]

    # 3) Fallback: regex scan for Redfin's JS data
    for script in soup.find_all("script"):
        text = script.string or ""
        if len(text) < 50:
            continue

        if not result["price"]:
            m = re.search(r'"price(?:Info)?"\s*:\s*\{[^}]*"amount"\s*:\s*(\d+)', text)
            if not m:
                m = re.search(r'"listingPrice"\s*:\s*(\d+)', text)
            if m:
                try:
                    result["price"] = int(m.group(1))
                except ValueError:
                    pass

        if not result["beds"]:
            m = re.search(r'"beds"\s*:\s*(\d+)', text)
            if m:
                result["beds"] = int(m.group(1))

        if not result["baths"]:
            m = re.search(r'"baths"\s*:\s*([\d.]+)', text)
            if m:
                result["baths"] = float(m.group(1))

        if not result["sqft"]:
            m = re.search(r'"sqFt"\s*:\s*\{[^}]*"value"\s*:\s*(\d+)', text)
            if not m:
                m = re.search(r'"sqftInfo"\s*:\s*\{[^}]*"amount"\s*:\s*(\d+)', text)
            if m:
                result["sqft"] = int(m.group(1))

        if not result["yearBuilt"]:
            m = re.search(r'"yearBuilt"\s*:\s*\{[^}]*"value"\s*:\s*(\d{4})', text)
            if m:
                result["yearBuilt"] = int(m.group(1))

        if not result["annualTax"]:
            m = re.search(r'"taxInfo"\s*:\s*\{[^}]*"amount"\s*:\s*(\d+)', text)
            if m:
                result["annualTax"] = int(m.group(1))

        if result["hoaFee"] == 0:
            m = re.search(r'"hoaDues"\s*:\s*\{[^}]*"amount"\s*:\s*(\d+)', text)
            if m:
                result["hoaFee"] = int(m.group(1))

    # Price might come as string "$350,000" — normalize
    if isinstance(result["price"], str):
        try:
            result["price"] = int(re.sub(r"[^\d]", "", result["price"]))
        except ValueError:
            result["price"] = None

    if result["address"] or result["price"]:
        return result
    return None


# ---------------------------------------------------------------------------
# Neighborhood Search — Redfin search page scraping
# ---------------------------------------------------------------------------

# Global semaphore: max 3 concurrent Playwright browsers for search
_search_semaphore = asyncio.Semaphore(3)

_REDFIN_SEARCH_JS = """
() => {
    const cards = document.querySelectorAll('.MapHomeCardReact, [class*="HomeCard"]');
    const results = [];
    const seen = new Set();

    // Helper: extract beds/baths/sqft from a text string
    function parseStats(t) {
        const b = t.match(/(\\d+)\\s*(?:beds?|bd|BR)\\b/i);
        const bt = t.match(/(\\d+\\.?\\d*)\\s*(?:baths?|ba)\\b/i);
        const s = t.match(/(\\d[\\d,]*)\\s*(?:sq|SF)\\b/i);
        return {
            beds: b ? parseInt(b[1]) : null,
            baths: bt ? parseFloat(bt[1]) : null,
            sqft: s ? parseInt(s[1].replace(/,/g, '')) : null
        };
    }

    cards.forEach(card => {
        const linkEl = card.querySelector('a[href*="/home/"]');
        const url = linkEl ? linkEl.href : null;
        if (!url || seen.has(url)) return;
        seen.add(url);

        // --- Price ---
        const priceDiv = card.querySelector('.bp-Homecard__Price, [class*="Price"]');
        let price = null;
        if (priceDiv) {
            const m = priceDiv.textContent.match(/\\$(\\d[\\d,]*)/);
            if (m) price = parseInt(m[1].replace(/,/g, ''));
        }

        // --- Address ---
        const addrEl = card.querySelector('.bp-Homecard__Address, [class*="homeAddressV2"], [class*="address"]');

        // --- Beds / Baths / Sqft ---
        let beds = null, baths = null, sqft = null;

        // Method 1: Dedicated stats element
        const statsEls = card.querySelectorAll('.bp-Homecard__Stats, [class*="HomeStats"], [class*="homeStat"], [class*="home-stat"], [class*="KeyStats"], [class*="keyStats"]');
        for (const el of statsEls) {
            const p = parseStats(el.textContent);
            if (p.beds !== null) beds = p.beds;
            if (p.baths !== null) baths = p.baths;
            if (p.sqft !== null) sqft = p.sqft;
            if (beds !== null) break;
        }

        // Method 2: Look for individual stat spans/divs inside the card
        if (beds === null) {
            const spans = card.querySelectorAll('span, div');
            for (const sp of spans) {
                const txt = sp.textContent.trim();
                // Match standalone "3 Beds" or "2 Baths" text nodes (short, focused)
                if (txt.length < 15) {
                    if (beds === null) {
                        const bm = txt.match(/^(\\d+)\\s*(?:beds?|bd|BR)$/i);
                        if (bm) beds = parseInt(bm[1]);
                    }
                    if (baths === null) {
                        const btm = txt.match(/^(\\d+\\.?\\d*)\\s*(?:baths?|ba)$/i);
                        if (btm) baths = parseFloat(btm[1]);
                    }
                    if (sqft === null) {
                        const sm = txt.match(/^(\\d[\\d,]*)\\s*(?:sq|SF)/i);
                        if (sm) sqft = parseInt(sm[1].replace(/,/g, ''));
                    }
                }
            }
        }

        // Method 3: Card aria-label or title attribute (Redfin sometimes puts stats here)
        if (beds === null) {
            const ariaEl = card.querySelector('[aria-label]');
            if (ariaEl) {
                const p = parseStats(ariaEl.getAttribute('aria-label'));
                if (p.beds !== null && p.beds <= 20) beds = p.beds;
                if (p.baths !== null && baths === null) baths = p.baths;
                if (p.sqft !== null && sqft === null) sqft = p.sqft;
            }
        }

        // Method 4: Full card text fallback (with sanity checks)
        if (beds === null) {
            const fullText = card.textContent;
            const p = parseStats(fullText);
            if (p.beds !== null && p.beds <= 20) beds = p.beds;
            if (p.baths !== null && p.baths <= 20 && baths === null) baths = p.baths;
            if (p.sqft !== null && sqft === null) sqft = p.sqft;
        }

        // --- Image ---
        const imgEl = card.querySelector('img[src*="cdn-redfin"], img[src*="photos"], img[src*="ssl.cdn"], img[src*="rdcpix"]');

        results.push({
            address: addrEl ? addrEl.textContent.trim() : null,
            price: price,
            beds: beds,
            baths: baths,
            sqft: sqft,
            listingUrl: url,
            imageUrl: imgEl ? imgEl.src : null
        });
    });
    return results;
}
"""


def _build_redfin_filter_path(filters: dict) -> str:
    """Build Redfin filter path segments from filters dict."""
    filter_parts = []
    if filters.get("min_price"):
        filter_parts.append(f"min-price={int(filters['min_price'])}")
    if filters.get("max_price"):
        filter_parts.append(f"max-price={int(filters['max_price'])}")
    if filters.get("min_beds") and filters["min_beds"] > 0:
        filter_parts.append(f"min-beds={int(filters['min_beds'])}")
    ptype_map = {"house": "house", "condo": "condo,townhouse", "multi-family": "multifamily"}
    if filters.get("property_type") and filters["property_type"] in ptype_map:
        filter_parts.append(f"property-type={ptype_map[filters['property_type']]}")
    if filters.get("sort") == "price-asc":
        filter_parts.append("sort=lo-price")
    if filter_parts:
        return "/filter/" + ",".join(filter_parts)
    return ""


def _build_redfin_search_url(location: str, filters: dict) -> str:
    """Build a Redfin search URL from location and filters.

    For zip codes, we can construct the URL directly.
    For city names, returns None — caller must use Playwright search bar.
    """
    query = location.strip()

    # Detect zip code (direct URL) vs city name (needs search)
    if re.match(r"^\d{5}$", query):
        base = f"https://www.redfin.com/zipcode/{query}"
        return base + _build_redfin_filter_path(filters)

    # City names can't be constructed as URLs (Redfin uses numeric city IDs)
    return None


async def _search_redfin_page(location: str, filters: dict) -> dict:
    """Search Redfin by loading the search results page with Playwright.

    Scrolls down multiple times to load more listings via lazy-loading.
    """
    from playwright.async_api import async_playwright

    direct_url = _build_redfin_search_url(location, filters)
    max_results = filters.get("max_results", 40)

    async with _search_semaphore, async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        if direct_url:
            # Zip code — navigate directly
            try:
                await page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                await browser.close()
                return {"error": "Could not connect to Redfin. Please try again later."}
        else:
            # City name — use Redfin search bar to resolve
            try:
                await page.goto("https://www.redfin.com", wait_until="domcontentloaded", timeout=20000)
                # Type in search box and pick first suggestion
                search_input = page.locator("input[type='text'][placeholder*='Search'], input[type='search'], #search-box-input, [data-testid='search-box-input']").first
                await search_input.fill(location.strip())
                await page.wait_for_timeout(1500)
                # Press Enter to search (autocomplete should resolve)
                await search_input.press("Enter")
                await page.wait_for_timeout(3000)
                # Now append filters to the URL
                current_url = page.url
                filter_path = _build_redfin_filter_path(filters)
                if filter_path and "/filter/" not in current_url:
                    await page.goto(current_url.rstrip("/") + filter_path, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                await browser.close()
                return {"error": "Could not connect to Redfin. Please try again later."}

        # Check for redirect to main page (bad location)
        final_url = page.url
        if "/zipcode/" not in final_url and "/city/" not in final_url and "/neighborhood/" not in final_url and "/filter/" not in final_url and "/county/" not in final_url and "/state/" not in final_url:
            await browser.close()
            return {"error": f'Could not find location "{location}". Try a zip code (e.g. "78701") or city + state (e.g. "Austin, TX").'}

        # Wait for listing cards to render
        try:
            await page.wait_for_selector(".MapHomeCardReact, [class*='HomeCard']", timeout=8000)
        except Exception:
            # No listings found or page didn't load cards
            html_text = await page.content()
            await browser.close()
            if "No results found" in html_text or "0 homes" in html_text:
                return {"listings": [], "total": 0}
            return {"error": "No listings found. Try adjusting your filters or searching a different area."}

        await page.wait_for_timeout(2000)

        # Scroll down to load more lazy-loaded listings
        # More scrolls = more listings. Stop early if no new content loaded.
        prev_count = 0
        for _ in range(8):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
            cur_count = await page.evaluate(
                "document.querySelectorAll('.MapHomeCardReact, [class*=\"HomeCard\"]').length"
            )
            if cur_count == prev_count and cur_count >= 10:
                break  # no new listings loaded
            prev_count = cur_count

        # Extract total count from page (e.g., "47 homes" in the results header)
        page_total = await page.evaluate("""
            () => {
                const el = document.querySelector('[class*="homes"], [class*="result"]');
                if (el) {
                    const m = el.textContent.match(/(\\d+)\\s*home/i);
                    if (m) return parseInt(m[1]);
                }
                return null;
            }
        """)

        # Extract location label from page title
        title = await page.title()
        label = location
        if title:
            # "78701, TX Real Estate & Homes for Sale | Redfin"
            # "Memphis, TN Homes for Sale & Real Estate | Redfin"
            label = re.sub(r"\s*\|.*$", "", title)
            label = re.sub(r"\s*(Real Estate|Homes for Sale|Houses for Sale|&).*$", "", label).strip()
            if not label:
                label = location

        listings = await page.evaluate(_REDFIN_SEARCH_JS)
        await browser.close()

    # Filter out listings without price and cap results
    listings = [l for l in listings if l.get("price")][:max_results]

    return {
        "listings": listings,
        "total": page_total or len(listings),
        "location_label": label,
    }


@app.post("/api/search")
async def search_neighborhood(request: Request):
    """Search for listings in a neighborhood/zip/city via Redfin."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"search:{client_ip}", 3):
        return JSONResponse(
            {"error": "Too many searches. Please wait a minute before trying again."},
            status_code=429,
        )

    body = await request.json()
    location = (body.get("location") or "").strip()
    if not location:
        return JSONResponse({"error": "Location is required."}, status_code=400)
    if len(location) > 200:
        return JSONResponse({"error": "Location query is too long."}, status_code=400)

    filters = {
        "min_price": body.get("min_price"),
        "max_price": body.get("max_price"),
        "min_beds": body.get("min_beds"),
        "property_type": body.get("property_type"),
        "max_results": min(body.get("max_results", 25), 75),
    }

    result = await _search_redfin_page(location, filters)

    if "error" in result and "listings" not in result:
        return JSONResponse({"error": result["error"]}, status_code=404)

    return JSONResponse(result)


@app.post("/api/smart-search")
async def smart_search(request: Request):
    """Smart Deal Finder: search listings + auto-estimate rent from market data.

    Strategy: fetch rentals first, compute a smart max price from rent data,
    then search for-sale listings within that price range so results are
    more likely to be viable investment deals.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"smart:{client_ip}", 3):
        return JSONResponse(
            {"error": "Too many searches. Please wait a minute before trying again."},
            status_code=429,
        )

    body = await request.json()
    location = (body.get("location") or "").strip()
    if not location:
        return JSONResponse({"error": "Location is required."}, status_code=400)
    if len(location) > 200:
        return JSONResponse({"error": "Location query is too long."}, status_code=400)

    user_min_beds = body.get("min_beds")
    user_property_type = body.get("property_type")
    min_price = body.get("min_price") or 25000
    user_max_results = body.get("max_results") or 50

    # Step 1+2: Fetch rental data AND for-sale listings IN PARALLEL
    # Use a generous max price for the initial search; we'll filter down
    # once we know the smart price cap from rental data.
    initial_filters = {
        "min_price": min_price,
        "max_price": 750000,  # generous cap; will narrow after rent data
        "min_beds": user_min_beds,
        "property_type": user_property_type or "house",
        "max_results": min(user_max_results + 20, 80),
        "sort": "price-asc",
    }

    # Run rentals, for-sale listings, AND mortgage rate fetch in parallel
    rental_beds = user_min_beds if user_min_beds and user_min_beds >= 2 else None
    rentals_task = asyncio.create_task(_search_redfin_rentals(location, rental_beds))
    listings_task = asyncio.create_task(_search_redfin_page(location, initial_filters))
    rate_task = asyncio.create_task(_ensure_mortgage_rate())
    rentals_result, listings_result, _ = await asyncio.gather(
        rentals_task, listings_task, rate_task
    )

    # Build rent lookup by bedroom count
    rent_by_beds: dict[int, list[int]] = {}
    all_rents: list[int] = []
    for r in rentals_result.get("rentals", []):
        rent_val = r.get("rent", 0)
        if rent_val <= 0:
            continue
        all_rents.append(rent_val)
        b = r.get("beds")
        if b is not None and b > 0:
            rent_by_beds.setdefault(b, []).append(rent_val)

    # Compute median rent per bedroom count, and also the 75th percentile
    rent_median_by_beds: dict[int, int] = {}
    rent_p75_by_beds: dict[int, int] = {}
    for beds, rents in rent_by_beds.items():
        rents.sort()
        rent_median_by_beds[beds] = rents[len(rents) // 2]
        rent_p75_by_beds[beds] = rents[min(int(len(rents) * 0.75), len(rents) - 1)]

    overall_median = 0
    overall_p75 = 0
    if all_rents:
        all_rents.sort()
        overall_median = all_rents[len(all_rents) // 2]
        overall_p75 = all_rents[min(int(len(all_rents) * 0.75), len(all_rents) - 1)]

    # Compute smart max price from rent data
    # Use median (not P75) to avoid luxury apartment skew.
    # Multiplier of 200 (~0.5% rent/price) is conservative for 7% rate
    # environment — deals above this ratio rarely cash-flow positive.
    smart_max_price = None
    if overall_median > 0:
        # Use overall median (not max across bedrooms) to avoid
        # inflated caps from high-bedroom luxury rentals.
        # Multiplier of 250 ≈ GRM 20.8, upper bound for viable investment deals.
        # See README "Smart Price Cap" section for the multiplier table.
        best_rent = overall_median
        smart_max_price = int(best_rent * 250)
        smart_max_price = ((smart_max_price + 24999) // 25000) * 25000
        smart_max_price = max(smart_max_price, 75000)

    if "error" in listings_result and "listings" not in listings_result:
        return JSONResponse({"error": listings_result["error"]}, status_code=404)

    # If no rental data at all, we can't score deals meaningfully
    if not all_rents:
        return JSONResponse(
            {"error": "No rental data found for this area. Try a nearby zip code — rent comps are needed to estimate deals."},
            status_code=404,
        )

    listings = listings_result.get("listings", [])

    # Filter by smart max price (initial search used generous $500K cap)
    if smart_max_price and listings:
        listings = [l for l in listings if l.get("price", 0) <= smart_max_price]

    if not listings:
        return JSONResponse(
            {"error": "No for-sale listings found. Try a different location."},
            status_code=404,
        )

    # Step 4: Filter out likely vacant parcels
    # Addresses starting with "0 " are empty land listings on Redfin
    listings = [
        l for l in listings
        if not (l.get("address") or "").strip().startswith("0 ")
    ]

    # Step 5: Attach estimated rent to each listing
    # Use bedroom-specific rent when available, otherwise find closest match.
    # Prefer a blend over bedroom-specific rent when it's >30% above the
    # overall median — likely skewed by luxury apartments.
    for listing in listings:
        beds = listing.get("beds")
        bed_rent = None
        if beds and beds in rent_median_by_beds:
            bed_rent = rent_median_by_beds[beds]
        elif beds and rent_median_by_beds:
            closest = min(rent_median_by_beds.keys(), key=lambda b: abs(b - beds))
            bed_rent = rent_median_by_beds[closest]

        if bed_rent and overall_median > 0:
            # If bedroom-specific rent is >30% above overall median, it may be
            # skewed by luxury apartments. Use a blend to moderate the estimate.
            if bed_rent > overall_median * 1.3:
                listing["estRent"] = int((bed_rent + overall_median) / 2)
            else:
                listing["estRent"] = bed_rent
        elif bed_rent:
            listing["estRent"] = bed_rent
        elif overall_median > 0:
            listing["estRent"] = overall_median
        else:
            listing["estRent"] = None

        # Sanity cap: rent shouldn't exceed 2% of price monthly (24% annual).
        # Even aggressive cash-flow markets rarely exceed 1.5%.
        # Floor of $500 ensures very cheap properties get usable estimates.
        price = listing.get("price") or 0
        if listing["estRent"] and price > 0:
            max_plausible_rent = max(int(price * 0.02), 500)
            listing["estRent"] = min(listing["estRent"], max_plausible_rent)

    # Cap to user's requested max
    listings = listings[:user_max_results]

    # Rent confidence: how reliable is the estimate?
    rent_count = len(all_rents)
    rent_confidence = "high" if rent_count >= 15 else "medium" if rent_count >= 5 else "low"

    # Include current mortgage rate for scoring calibration
    current_rate = _mortgage_rate_cache.get("rate")

    return JSONResponse({
        "listings": listings,
        "total": listings_result.get("total", len(listings)),
        "location_label": listings_result.get("location_label", location),
        "rent_stats": rentals_result.get("stats"),
        "rent_by_beds": {str(k): v for k, v in rent_median_by_beds.items()},
        "smart_max_price": smart_max_price,
        "rent_confidence": rent_confidence,
        "mortgage_rate": current_rate,
    })


# ---------------------------------------------------------------------------
# Mortgage Rate — FRED API (free, no key required for this endpoint)
# ---------------------------------------------------------------------------
_mortgage_rate_cache: dict = {"rate": None, "fetched_at": 0}


async def _ensure_mortgage_rate() -> float | None:
    """Fetch and cache mortgage rate if not already cached. Returns the rate."""
    now = time.time()
    if _mortgage_rate_cache["rate"] is not None and now - _mortgage_rate_cache["fetched_at"] < 21600:
        return _mortgage_rate_cache["rate"]
    try:
        hdrs = {k: v for k, v in HEADERS.items() if k != "Accept-Encoding"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.freddiemac.com/pmms", headers=hdrs)
            if resp.status_code == 200:
                match = re.search(r"(\d+\.\d+)%", resp.text)
                if match:
                    rate = float(match.group(1))
                    if 2.0 <= rate <= 15.0:
                        _mortgage_rate_cache["rate"] = rate
                        _mortgage_rate_cache["fetched_at"] = now
                        return rate
    except Exception:
        pass
    return _mortgage_rate_cache.get("rate")


@app.get("/api/mortgage-rate")
async def get_mortgage_rate():
    """Fetch current average 30-year fixed mortgage rate from FRED."""
    rate = await _ensure_mortgage_rate()
    if rate is not None:
        return JSONResponse({"rate": rate})
    return JSONResponse({"rate": None, "error": "Could not fetch current rate."})


# ---------------------------------------------------------------------------
# Rent Estimation — Redfin rental listings search
# ---------------------------------------------------------------------------
_REDFIN_RENT_JS = """
() => {
    const cards = document.querySelectorAll('.MapHomeCardReact, [class*="HomeCard"]');
    const results = [];
    const seen = new Set();
    cards.forEach(card => {
        const priceDiv = card.querySelector('.bp-Homecard__Price, [class*="Price"]');
        if (!priceDiv) return;
        const priceText = priceDiv.textContent;
        // Only include rental listings (contain /mo or /month)
        if (!/\\/mo/i.test(priceText) && !/rent/i.test(priceText)) {
            // Also check if it looks like a rent price (< $10k/mo typically)
            const m = priceText.match(/\\$(\\d[\\d,]*)/);
            if (m) {
                const p = parseInt(m[1].replace(/,/g, ''));
                if (p > 15000) return; // likely a sale price, skip
            }
        }
        const m = priceText.match(/\\$(\\d[\\d,]*)/);
        if (!m) return;
        const rent = parseInt(m[1].replace(/,/g, ''));
        if (rent <= 0 || rent > 50000) return;

        let beds = null, baths = null, sqft = null;
        // Try multiple stat selectors
        const statsEls = card.querySelectorAll('.bp-Homecard__Stats, [class*="HomeStats"], [class*="homeStat"], [class*="KeyStats"]');
        for (const el of statsEls) {
            const t = el.textContent;
            const bM = t.match(/(\\d+)\\s*(?:beds?|bd|BR)\\b/i);
            const btM = t.match(/(\\d+\\.?\\d*)\\s*(?:baths?|ba)\\b/i);
            const sM = t.match(/(\\d[\\d,]*)\\s*(?:sq|SF)\\b/i);
            if (bM) beds = parseInt(bM[1]);
            if (btM) baths = parseFloat(btM[1]);
            if (sM) sqft = parseInt(sM[1].replace(/,/g, ''));
            if (beds !== null) break;
        }
        // Fallback: individual short spans
        if (beds === null) {
            const spans = card.querySelectorAll('span, div');
            for (const sp of spans) {
                const txt = sp.textContent.trim();
                if (txt.length < 15) {
                    if (beds === null) { const bm = txt.match(/^(\\d+)\\s*(?:beds?|bd|BR)$/i); if (bm) beds = parseInt(bm[1]); }
                    if (baths === null) { const btm = txt.match(/^(\\d+\\.?\\d*)\\s*(?:baths?|ba)$/i); if (btm) baths = parseFloat(btm[1]); }
                }
            }
        }
        const addrEl = card.querySelector('.bp-Homecard__Address, [class*="homeAddressV2"]');
        const addr = addrEl ? addrEl.textContent.trim() : null;
        const key = addr || rent.toString();
        if (seen.has(key)) return;
        seen.add(key);
        results.push({ rent: rent, beds: beds, baths: baths, sqft: sqft, address: addr });
    });
    return results;
}
"""


async def _search_redfin_rentals(location: str, beds: int | None = None) -> dict:
    """Search Redfin for rental listings to estimate market rent.

    For zip codes, navigates directly. For city names, uses Playwright
    search bar (Redfin uses numeric city IDs that can't be URL-constructed).
    """
    from playwright.async_api import async_playwright

    query = location.strip()
    is_zip = bool(re.match(r"^\d{5}$", query))

    # Build bed filter suffix
    bed_filter = ""
    if beds and beds > 0:
        bed_filter = f"/filter/min-beds={int(beds)},max-beds={int(beds)}"

    async with _search_semaphore, async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        if is_zip:
            # Zip code — navigate directly
            base = f"https://www.redfin.com/zipcode/{query}/apartments-for-rent{bed_filter}"
            try:
                await page.goto(base, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                await browser.close()
                return {"error": "Could not connect to Redfin."}
        else:
            # City name — use Redfin search bar to resolve, then switch to rentals
            try:
                await page.goto("https://www.redfin.com", wait_until="domcontentloaded", timeout=20000)
                search_input = page.locator(
                    "input[type='text'][placeholder*='Search'], input[type='search'], "
                    "#search-box-input, [data-testid='search-box-input']"
                ).first
                await search_input.fill(query)
                await page.wait_for_timeout(1500)
                await search_input.press("Enter")
                await page.wait_for_timeout(3000)
                # Now on the for-sale page; switch to rentals
                current_url = page.url
                # Replace for-sale path with rental path
                rental_url = re.sub(
                    r"(/filter/.*)?$", "/apartments-for-rent" + bed_filter, current_url.rstrip("/")
                )
                if "/apartments-for-rent" not in rental_url:
                    rental_url = current_url.rstrip("/") + "/apartments-for-rent" + bed_filter
                await page.goto(rental_url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                await browser.close()
                return {"error": "Could not connect to Redfin."}

        try:
            await page.wait_for_selector(
                ".MapHomeCardReact, [class*='HomeCard']", timeout=8000
            )
        except Exception:
            await browser.close()
            return {"rentals": [], "total": 0}

        await page.wait_for_timeout(1500)

        # Scroll to load more rental listings
        for _ in range(4):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)

        rentals = await page.evaluate(_REDFIN_RENT_JS)
        await browser.close()

    rentals = [r for r in rentals if r.get("rent") and r["rent"] > 0][:40]
    if not rentals:
        return {"rentals": [], "total": 0}

    rents = [r["rent"] for r in rentals]
    rents.sort()
    avg_rent = sum(rents) / len(rents)
    median_rent = rents[len(rents) // 2]
    low_rent = rents[int(len(rents) * 0.25)] if len(rents) >= 4 else rents[0]
    high_rent = rents[int(len(rents) * 0.75)] if len(rents) >= 4 else rents[-1]

    return {
        "rentals": rentals[:15],
        "total": len(rentals),
        "stats": {
            "avg": round(avg_rent),
            "median": round(median_rent),
            "low": round(low_rent),
            "high": round(high_rent),
            "count": len(rents),
        },
    }


@app.post("/api/rent-estimate")
async def estimate_rent(request: Request):
    """Estimate market rent for a location using Redfin rental listings."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"rent:{client_ip}", 3):
        return JSONResponse(
            {"error": "Too many requests. Please wait a minute."},
            status_code=429,
        )

    body = await request.json()
    location = (body.get("location") or "").strip()
    if not location:
        return JSONResponse({"error": "Location is required."}, status_code=400)
    if len(location) > 200:
        return JSONResponse({"error": "Location too long."}, status_code=400)

    beds = body.get("beds")
    result = await _search_redfin_rentals(location, beds)

    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=404)

    return JSONResponse(result)


@app.post("/api/scrape")
async def scrape_property(request: Request):
    # Rate limit: 5 requests per minute per IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"scrape:{client_ip}", 5):
        return JSONResponse({"error": "Too many requests. Please wait a minute before trying again."}, status_code=429)

    body, error_response = await _parse_json_object(request)
    if error_response:
        return error_response

    url = str(body.get("url", "")).strip()

    # --- Validate URL ---
    if not url:
        return JSONResponse({"error": "URL is required."}, status_code=400)

    if len(url) > 2000:
        return JSONResponse({"error": "URL is too long."}, status_code=400)

    parsed = urlparse(url)
    source = _detect_source(parsed.hostname)

    if source == "unknown":
        return JSONResponse(
            {"error": "Unsupported URL. Paste a Zillow or Redfin listing URL."},
            status_code=400,
        )

    # Source-specific path validation
    if source == "zillow" and not re.search(r"/homedetails/|/zpid_|/homes/", parsed.path or ""):
        return JSONResponse(
            {"error": "Please provide a direct Zillow property listing URL (e.g. zillow.com/homedetails/...)."},
            status_code=400,
        )
    if source == "redfin" and not re.search(r"/home/\d+", parsed.path or ""):
        return JSONResponse(
            {"error": "Please provide a direct Redfin property listing URL (e.g. redfin.com/.../home/12345)."},
            status_code=400,
        )

    # --- Fetch page (try httpx first, fallback to Playwright) ---
    html_text = None
    site_label = "Redfin" if source == "redfin" else "Zillow"

    # Attempt 1: httpx (fast, but Zillow often blocks this)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=HEADERS)
        if resp.status_code < 400 and "captcha" not in resp.text[:2000].lower():
            # Check for bot-block pages (Zillow returns 200 with "Access denied")
            if "access to this page has been denied" not in resp.text[:3000].lower():
                html_text = resp.text
    except httpx.RequestError:
        pass

    # Attempt 2: Playwright headless browser
    if html_text is None:
        try:
            html_text = await _fetch_with_playwright(url)
            # Check for bot block even in Playwright response
            if html_text and "access to this page has been denied" in html_text[:3000].lower():
                html_text = None
        except Exception:
            pass

    if not html_text:
        return JSONResponse(
            {"error": f"Could not fetch the {site_label} page. The site may be blocking automated requests. Try again later or enter data manually."},
            status_code=503,
        )

    # --- Parse HTML ---
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        return JSONResponse(
            {"error": "Failed to parse the page HTML. The page may be malformed."},
            status_code=422,
        )

    # Check for CAPTCHA / bot block pages
    if (soup.find("div", class_="captcha-container")
            or "captcha" in html_text[:2000].lower()
            or "access to this page has been denied" in html_text[:3000].lower()):
        return JSONResponse(
            {"error": f"{site_label} blocked the request. Please try again later or enter data manually."},
            status_code=503,
        )

    # --- Extract property data ---
    if source == "redfin":
        result = _extract_redfin(soup)
        if result:
            return JSONResponse(result)
        return JSONResponse(
            {"error": "Could not extract property data from this Redfin listing."},
            status_code=422,
        )

    # Zillow extraction strategies
    result = _extract_from_next_data(soup)
    if result:
        return JSONResponse(result)

    result = _extract_from_ld_json(soup)
    if result:
        return JSONResponse(result)

    result = _extract_from_dom(soup)
    if result:
        return JSONResponse(result)

    return JSONResponse(
        {"error": "Could not extract property data. Zillow may have changed their page structure. Try using a Redfin URL instead, or enter data manually."},
        status_code=422,
    )


AI_SYSTEM_PROMPT = (
    "You are a real estate investment analyst. Analyze this rental "
    "property deal and provide a plain-English investment summary "
    "with: 1) Overall Assessment, 2) Key Strengths, 3) Key Risks, "
    "4) Recommendation. Be concise but thorough. "
    "Jump straight to the analysis."
)


def _strip_thinking(text: str) -> str:
    """Remove thinking/reasoning blocks from LLM output."""
    # Strip <think>...</think> blocks (qwen3, deepseek-r1)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip plain-text thinking blocks that appear before the actual analysis.
    # Look for the first analysis header pattern and discard everything before it.
    header = re.search(
        r"^(#{1,3}\s+|\*\*\s*|\d+[\.\)]\s*\*\*\s*)"
        r"(Overall|Investment|Key Strength|Key Risk|Recommendation|Summary|Assessment|Analysis)",
        text, re.MULTILINE | re.IGNORECASE,
    )
    if header and header.start() > 100:
        text = text[header.start():].strip()
    return text


async def _analyze_with_ollama(metrics: str, model_override: str | None = None) -> str:
    """Call local Ollama API."""
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = model_override or os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{ollama_url}/api/chat",
            json={
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": metrics},
                ],
                "stream": False,
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Ollama error: {resp.status_code} - {resp.text[:200]}")
    data = resp.json()
    return _strip_thinking(data["message"]["content"])


async def _analyze_with_lmstudio(metrics: str, model_override: str | None = None) -> str:
    """Call LM Studio's OpenAI-compatible API."""
    lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
    lmstudio_model = model_override or os.getenv("LMSTUDIO_MODEL", "")  # empty = use whatever is loaded
    async with httpx.AsyncClient(timeout=300) as client:
        payload = {
            "messages": [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": metrics},
            ],
            "temperature": 0.7,
            "max_tokens": 8192,
            "stream": False,
        }
        if lmstudio_model:
            payload["model"] = lmstudio_model
        resp = await client.post(
            f"{lmstudio_url}/v1/chat/completions",
            json=payload,
        )
    if resp.status_code != 200:
        raise Exception(f"LM Studio error: {resp.status_code} - {resp.text[:200]}")
    data = resp.json()
    return _strip_thinking(data["choices"][0]["message"]["content"])


async def _analyze_with_anthropic(metrics: str, api_key: str, model_override: str | None = None) -> str:
    """Call Anthropic Claude API."""
    anthropic_model = model_override or "claude-sonnet-4-20250514"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": anthropic_model,
                "max_tokens": 1024,
                "system": AI_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": metrics}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Anthropic API error (HTTP {resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    return data["content"][0]["text"]


def _resolve_provider():
    """Return (provider, api_key) based on env configuration."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    provider = os.getenv("AI_PROVIDER", "auto").lower()
    return provider, api_key


@app.post("/api/analyze-ai")
async def analyze_ai(request: Request):
    # Rate limit: 10 requests per minute per IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"ai:{client_ip}", 10):
        return JSONResponse({"error": "Too many requests. Please wait before trying again."}, status_code=429)

    body, error_response = await _parse_json_object(request)
    if error_response:
        return error_response

    metrics = body.get("metrics", "")
    model = body.get("model")  # optional model override
    if not metrics:
        return JSONResponse(
            {"error": "Missing 'metrics' in request body."},
            status_code=400,
        )
    if len(metrics) > 50_000:
        return JSONResponse(
            {"error": "Input too large."},
            status_code=400,
        )

    # Determine AI provider
    provider, api_key = _resolve_provider()

    # LM Studio provider
    if provider == "lmstudio":
        try:
            text = await _analyze_with_lmstudio(metrics, model_override=model)
            return JSONResponse({"analysis": text, "provider": "lmstudio"})
        except Exception as exc:
            return JSONResponse(
                {"error": f"LM Studio is not running. Start LM Studio and load a model, then enable the local server.\n\nError: {exc}"},
                status_code=502,
            )

    # Auto mode: try lmstudio first, then ollama, then anthropic
    if provider == "auto":
        # Try LM Studio
        lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                probe = await client.get(f"{lmstudio_url}/v1/models")
            if probe.status_code == 200:
                try:
                    text = await _analyze_with_lmstudio(metrics, model_override=model)
                    return JSONResponse({"analysis": text, "provider": "lmstudio"})
                except Exception:
                    pass
        except Exception:
            pass

    # Ollama provider (explicit or auto-detected)
    if provider == "ollama" or (provider == "auto" and not api_key):
        try:
            text = await _analyze_with_ollama(metrics, model_override=model)
            return JSONResponse({"analysis": text, "provider": "ollama"})
        except Exception as exc:
            if api_key:
                pass  # fall through to Anthropic
            else:
                return JSONResponse(
                    {"error": f"Ollama is not running or model not available. Start Ollama with: ollama serve\nThen pull a model: ollama pull {os.getenv('OLLAMA_MODEL', 'llama3.2:3b')}\n\nError: {exc}"},
                    status_code=502,
                )

    if not api_key:
        return JSONResponse(
            {"error": f"No AI provider configured. Either:\n1) Set ANTHROPIC_API_KEY in .env (paid)\n2) Run LM Studio locally (free): set AI_PROVIDER=lmstudio\n3) Run Ollama locally (free): ollama serve && ollama pull {os.getenv('OLLAMA_MODEL', 'llama3.2:3b')}"},
            status_code=400,
        )

    try:
        text = await _analyze_with_anthropic(metrics, api_key, model_override=model)
        return JSONResponse({"analysis": text, "provider": "anthropic"})
    except (httpx.RequestError, httpx.TimeoutException):
        return JSONResponse(
            {"error": "Could not reach AI service. Check your connection and try again."},
            status_code=502,
        )
    except Exception as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=502,
        )


# ---------------------------------------------------------------------------
# GET /api/models — list available models from the configured AI provider
# ---------------------------------------------------------------------------

ANTHROPIC_MODELS = [
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
    {"id": "claude-haiku-4-20250414", "name": "Claude Haiku 4"},
    {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
]


async def _get_lmstudio_models() -> dict | None:
    """Fetch models from LM Studio. Returns dict or None on failure."""
    lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
    current = os.getenv("LMSTUDIO_MODEL", "")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{lmstudio_url}/v1/models")
        if resp.status_code != 200:
            return None
        data = resp.json()
        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            # Filter out embedding models
            if "embed" in mid.lower():
                continue
            models.append({"id": mid, "name": mid})
        if not current and models:
            current = models[0]["id"]
        return {"provider": "lmstudio", "models": models, "current": current}
    except Exception:
        return None


async def _get_ollama_models() -> dict | None:
    """Fetch models from Ollama. Returns dict or None on failure."""
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    current = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            return None
        data = resp.json()
        models = []
        for m in data.get("models", []):
            mid = m.get("name", "") or m.get("model", "")
            models.append({"id": mid, "name": mid})
        return {"provider": "ollama", "models": models, "current": current}
    except Exception:
        return None


def _get_anthropic_models() -> dict | None:
    """Return hardcoded Anthropic models if API key is set."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return {
        "provider": "anthropic",
        "models": ANTHROPIC_MODELS,
        "current": "claude-sonnet-4-20250514",
    }


@app.get("/api/models")
async def list_models():
    provider, api_key = _resolve_provider()

    if provider == "lmstudio":
        result = await _get_lmstudio_models()
        if result:
            return JSONResponse(result)
        return JSONResponse({"error": "LM Studio is not reachable."}, status_code=502)

    if provider == "ollama":
        result = await _get_ollama_models()
        if result:
            return JSONResponse(result)
        return JSONResponse({"error": "Ollama is not reachable."}, status_code=502)

    if provider == "anthropic":
        result = _get_anthropic_models()
        if result:
            return JSONResponse(result)
        return JSONResponse({"error": "ANTHROPIC_API_KEY is not set."}, status_code=400)

    # auto: try lmstudio -> ollama -> anthropic
    result = await _get_lmstudio_models()
    if result:
        return JSONResponse(result)

    result = await _get_ollama_models()
    if result:
        return JSONResponse(result)

    result = _get_anthropic_models()
    if result:
        return JSONResponse(result)

    return JSONResponse(
        {"error": "No AI provider available."},
        status_code=502,
    )


# ---------------------------------------------------------------------------
# POST /api/analyze-ai-stream — SSE streaming version of analyze-ai
# ---------------------------------------------------------------------------

async def _stream_lmstudio(metrics: str, model_override: str | None = None):
    """Stream from LM Studio's OpenAI-compatible API."""
    lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
    lmstudio_model = model_override or os.getenv("LMSTUDIO_MODEL", "")

    payload = {
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": metrics},
        ],
        "temperature": 0.7,
        "max_tokens": 8192,
        "stream": True,
    }
    if lmstudio_model:
        payload["model"] = lmstudio_model

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST", f"{lmstudio_url}/v1/chat/completions", json=payload
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield f"data: {json.dumps({'error': f'LM Studio error: {resp.status_code} - {body[:200].decode()}'})}\n\n"
                return
            buffer = ""
            in_think = False
            found_header = False
            pending = ""
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                token = delta.get("content", "")
                if not token:
                    continue

                # Strip thinking: handle <think> tags
                processed = _process_stream_token(token, buffer, in_think, found_header, pending)
                buffer = processed["buffer"]
                in_think = processed["in_think"]
                found_header = processed["found_header"]
                pending = processed["pending"]
                if processed["output"]:
                    yield f"data: {json.dumps({'token': processed['output'], 'done': False})}\n\n"
    yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"


async def _stream_ollama(metrics: str, model_override: str | None = None):
    """Stream from Ollama API."""
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = model_override or os.getenv("OLLAMA_MODEL", "llama3.2:3b")

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": metrics},
        ],
        "stream": True,
    }

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST", f"{ollama_url}/api/chat", json=payload
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield f"data: {json.dumps({'error': f'Ollama error: {resp.status_code} - {body[:200].decode()}'})}\n\n"
                return
            buffer = ""
            in_think = False
            found_header = False
            pending = ""
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if not token:
                    if chunk.get("done"):
                        break
                    continue

                processed = _process_stream_token(token, buffer, in_think, found_header, pending)
                buffer = processed["buffer"]
                in_think = processed["in_think"]
                found_header = processed["found_header"]
                pending = processed["pending"]
                if processed["output"]:
                    yield f"data: {json.dumps({'token': processed['output'], 'done': False})}\n\n"
    yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"


async def _stream_anthropic(metrics: str, api_key: str, model_override: str | None = None):
    """Stream from Anthropic API."""
    anthropic_model = model_override or "claude-sonnet-4-20250514"
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": anthropic_model,
                "max_tokens": 1024,
                "system": AI_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": metrics}],
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield f"data: {json.dumps({'error': f'Anthropic error: {resp.status_code} - {body[:200].decode()}'})}\n\n"
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if not data_str:
                    continue
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                event_type = chunk.get("type", "")
                if event_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    token = delta.get("text", "")
                    if token:
                        yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"
                elif event_type == "message_stop":
                    break
    yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"


def _process_stream_token(
    token: str, buffer: str, in_think: bool, found_header: bool, pending: str
) -> dict:
    """Process a streaming token, stripping thinking blocks.

    Returns dict with keys: output, buffer, in_think, found_header, pending.
    """
    output = ""
    full = buffer + token

    # Handle <think> tags
    while True:
        if in_think:
            end_idx = full.find("</think>")
            if end_idx == -1:
                # Still inside think block, consume everything
                return {"output": output, "buffer": "", "in_think": True, "found_header": found_header, "pending": pending}
            else:
                full = full[end_idx + 8:]
                in_think = False
        else:
            start_idx = full.find("<think>")
            if start_idx != -1:
                # Text before <think> is real content
                before = full[:start_idx]
                if before:
                    pending += before
                full = full[start_idx + 7:]
                in_think = True
            else:
                break

    pending += full

    # If we haven't found the analysis header yet, check if the pending text
    # has enough content to determine it starts with "Thinking Process" or similar.
    if not found_header:
        # Check if the pending text contains an analysis header
        header = re.search(
            r"^(#{1,3}\s+|\*\*\s*|\d+[\.\)]\s*\*\*\s*)"
            r"(Overall|Investment|Key Strength|Key Risk|Recommendation|Summary|Assessment|Analysis)",
            pending, re.MULTILINE | re.IGNORECASE,
        )
        if header and header.start() > 100:
            # There's a thinking preamble — skip it
            pending = pending[header.start():]
            found_header = True
            output += pending
            pending = ""
        elif header:
            # Header found near the start — this is real content
            found_header = True
            output += pending
            pending = ""
        elif len(pending) > 300:
            # We've buffered enough without finding a thinking preamble, just emit
            found_header = True
            output += pending
            pending = ""
        # else: keep buffering
    else:
        output += pending
        pending = ""

    return {"output": output, "buffer": "", "in_think": in_think, "found_header": found_header, "pending": pending}


@app.post("/api/analyze-ai-stream")
async def analyze_ai_stream(request: Request):
    # Rate limit: 10 requests per minute per IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"ai-stream:{client_ip}", 10):
        return JSONResponse({"error": "Too many requests. Please wait before trying again."}, status_code=429)

    body, error_response = await _parse_json_object(request)
    if error_response:
        return error_response

    metrics = body.get("metrics", "")
    model = body.get("model")  # optional model override
    if not metrics:
        return JSONResponse(
            {"error": "Missing 'metrics' in request body."},
            status_code=400,
        )
    if len(metrics) > 50_000:
        return JSONResponse(
            {"error": "Input too large."},
            status_code=400,
        )

    provider, api_key = _resolve_provider()

    async def _pick_generator():
        # LM Studio explicit
        if provider == "lmstudio":
            return _stream_lmstudio(metrics, model_override=model)

        # Auto: try lmstudio first
        if provider == "auto":
            lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    probe = await client.get(f"{lmstudio_url}/v1/models")
                if probe.status_code == 200:
                    return _stream_lmstudio(metrics, model_override=model)
            except Exception:
                pass

        # Ollama explicit or auto fallback
        if provider == "ollama" or (provider == "auto" and not api_key):
            return _stream_ollama(metrics, model_override=model)

        # Anthropic
        if api_key:
            return _stream_anthropic(metrics, api_key, model_override=model)

        return None

    gen = await _pick_generator()
    if gen is None:
        return JSONResponse(
            {"error": f"No AI provider available. Configure one in .env."},
            status_code=400,
        )

    async def _with_timeout(generator, timeout_seconds=300):
        """Wrap a streaming generator with a timeout."""
        try:
            async for chunk in generator:
                yield chunk
        except asyncio.CancelledError:
            yield f"data: {json.dumps({'error': 'Stream timed out.'})}\n\n"

    return StreamingResponse(
        _with_timeout(gen),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def open_browser():
    webbrowser.open("http://localhost:8000")


if __name__ == "__main__":
    threading.Timer(1.5, open_browser).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
