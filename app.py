import os, json, re, webbrowser, threading
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
import uvicorn

load_dotenv()
app = FastAPI()

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


# ---------------------------------------------------------------------------
# Playwright fallback fetcher
# ---------------------------------------------------------------------------

async def _fetch_with_playwright(url: str) -> str:
    """Use a headless browser to fetch the page (bypasses bot detection)."""
    import asyncio
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait a moment for JS to populate __NEXT_DATA__
        await page.wait_for_timeout(2000)
        html = await page.content()
        await browser.close()
    return html


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return Path("index.html").read_text(encoding="utf-8")


@app.post("/api/scrape")
async def scrape_zillow(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()

    # --- Validate URL ---
    if not url:
        return JSONResponse({"error": "URL is required."}, status_code=400)

    parsed = urlparse(url)
    if not parsed.hostname or not parsed.hostname.endswith("zillow.com"):
        return JSONResponse(
            {"error": "Invalid URL. Only Zillow URLs are supported (must be a zillow.com link)."},
            status_code=400,
        )

    # --- Fetch page (try httpx first, fallback to Playwright) ---
    html_text = None

    # Attempt 1: httpx (fast, but often blocked)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=HEADERS)
        if resp.status_code < 400 and "captcha" not in resp.text[:2000].lower():
            html_text = resp.text
    except httpx.RequestError:
        pass

    # Attempt 2: Playwright headless browser (slower, but bypasses blocks)
    if html_text is None:
        try:
            html_text = await _fetch_with_playwright(url)
        except Exception as exc:
            return JSONResponse(
                {"error": f"Could not fetch the Zillow page. Both direct and browser methods failed. Try again later or enter data manually."},
                status_code=503,
            )

    if not html_text:
        return JSONResponse(
            {"error": "Could not reach Zillow. Check your internet connection and try again."},
            status_code=502,
        )

    # --- Parse HTML ---
    soup = BeautifulSoup(html_text, "lxml")

    # Check for CAPTCHA page
    if soup.find("div", class_="captcha-container") or "captcha" in html_text[:2000].lower():
        return JSONResponse(
            {"error": "Zillow returned a CAPTCHA page. Please try again later or use a different network."},
            status_code=503,
        )

    # --- Extract property data ---
    result = _extract_from_next_data(soup)
    if result:
        return JSONResponse(result)

    result = _extract_from_ld_json(soup)
    if result:
        return JSONResponse(result)

    return JSONResponse(
        {"error": "Could not extract property data. Zillow may have changed their page structure, or this listing type is not supported."},
        status_code=422,
    )


AI_SYSTEM_PROMPT = (
    "You are a real estate investment analyst. Analyze this rental "
    "property deal and provide a plain-English investment summary "
    "with: 1) Overall Assessment, 2) Key Strengths, 3) Key Risks, "
    "4) Recommendation. Be concise but thorough. "
    "Do NOT include internal reasoning, thinking process, or chain-of-thought. "
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
    body = await request.json()
    metrics = body.get("metrics", "")
    model = body.get("model")  # optional model override
    if not metrics:
        return JSONResponse(
            {"error": "Missing 'metrics' in request body."},
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
    body = await request.json()
    metrics = body.get("metrics", "")
    model = body.get("model")  # optional model override
    if not metrics:
        return JSONResponse(
            {"error": "Missing 'metrics' in request body."},
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

    return StreamingResponse(
        gen,
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
