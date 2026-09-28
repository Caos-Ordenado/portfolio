from contextlib import asynccontextmanager
import asyncio
import os
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import ClientTimeout
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from shared.interfaces.renderer import RendererScreenshotRequest
from shared.interfaces.web_crawler import CrawlRequest, VisionExtractRequest

from shared.logging import setup_logger

logger = setup_logger("openwebui_tools")


def _get_env(name: str, default: str) -> str:
    value = (os.getenv(name) or default).rstrip("/")
    return value


CRAWLER_BASE_URL = _get_env("CRAWLER_BASE_URL", "http://web-crawler.default.svc.cluster.local:8000")
RENDERER_BASE_URL = _get_env("RENDERER_BASE_URL", "http://renderer.default.svc.cluster.local:8000")
HTTP_TIMEOUT_SECONDS = int(os.getenv("OPENWEBUI_TOOLS_HTTP_TIMEOUT_SECONDS", "180"))


def _normalize_result(endpoint: str, data: Any) -> Any:
    """Make responses chat-friendly by avoiding huge payloads."""
    if not isinstance(data, dict):
        return data

    # Renderer screenshot: avoid returning huge base64 to chat
    if "screenshot_b64" in data:
        b64 = data.get("screenshot_b64") or ""
        data = dict(data)
        data.pop("screenshot_b64", None)
        data["screenshot_b64_len"] = len(b64)
        data["screenshot_b64_preview"] = b64[:120] if b64 else ""
        return data

    # Renderer HTML: omit full HTML, keep text
    if "html" in data:
        html = data.get("html") or ""
        data = dict(data)
        data.pop("html", None)
        data["html_len"] = len(html)
        return data

    # Crawler: cap results
    if endpoint == "crawl" and "results" in data and isinstance(data.get("results"), list):
        results = data.get("results") or []
        data = dict(data)
        data["results_total"] = len(results)
        data["results"] = results[:5]
        return data

    return data


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown hooks (replaces the deprecated @app.on_event handlers)."""
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(lifespan=lifespan, title="Open WebUI Tools Proxy", version="0.1.0")


async def _startup() -> None:
    # NOTE: extract-vision can take >20s (renderer + vision model), so keep this generous.
    timeout = ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    app.state.http = aiohttp.ClientSession(timeout=timeout)
    logger.info(
        f"openwebui-tools starting | crawler={CRAWLER_BASE_URL} | renderer={RENDERER_BASE_URL} | http_timeout_s={HTTP_TIMEOUT_SECONDS}"
    )


async def _shutdown() -> None:
    session: Optional[aiohttp.ClientSession] = getattr(app.state, "http", None)
    if session:
        await session.close()


@app.get("/health")
async def health() -> Dict[str, str]:
    """Report readiness for the internal tools service."""
    return {"status": "ok"}


async def _forward_json(method: str, url: str, payload: Any) -> Dict[str, Any]:
    session: aiohttp.ClientSession = app.state.http
    logger.debug(f"Forward {method} -> {url}")

    try:
        async with session.request(method, url, json=payload) as resp:
            text = await resp.text()
            if resp.status < 200 or resp.status >= 300:
                logger.warning(f"Upstream error {resp.status} for {url}: {text[:300]}")
                return {"success": False, "data": None, "error": f"{resp.status}: {text[:2000]}"}
            # Prefer JSON, but tolerate non-JSON
            try:
                data = await resp.json()
            except Exception:
                data = {"raw_text": text}
            return {"success": True, "data": data, "error": None}
    except asyncio.TimeoutError:
        # asyncio.TimeoutError stringifies to "" — return a useful error for UI/debugging.
        msg = f"timeout after {HTTP_TIMEOUT_SECONDS}s calling {url}"
        logger.warning(msg)
        return {"success": False, "data": None, "error": msg}
    except Exception as e:
        logger.exception(f"Request failed for {url}")
        return {"success": False, "data": None, "error": str(e)}


@app.post("/crawl")
async def crawl(request: CrawlRequest) -> JSONResponse:
    payload = request.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    res = await _forward_json("POST", f"{CRAWLER_BASE_URL}/crawl", payload)
    if res["success"]:
        res["data"] = _normalize_result("crawl", res["data"])
    return JSONResponse(res)


@app.post("/render-html")
async def render_html(request: RendererScreenshotRequest) -> JSONResponse:
    payload = request.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    res = await _forward_json("POST", f"{RENDERER_BASE_URL}/render-html", payload)
    if res["success"]:
        res["data"] = _normalize_result("render-html", res["data"])
    return JSONResponse(res)


@app.post("/screenshot")
async def screenshot(request: RendererScreenshotRequest) -> JSONResponse:
    payload = request.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    res = await _forward_json("POST", f"{RENDERER_BASE_URL}/screenshot", payload)
    if res["success"]:
        res["data"] = _normalize_result("screenshot", res["data"])
    return JSONResponse(res)


@app.post("/extract-vision")
async def extract_vision(request: VisionExtractRequest) -> JSONResponse:
    payload = request.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    res = await _forward_json("POST", f"{CRAWLER_BASE_URL}/extract-vision", payload)
    # Response is already small JSON; no special normalization required
    return JSONResponse(res)


