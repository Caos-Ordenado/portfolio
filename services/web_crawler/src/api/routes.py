from fastapi import APIRouter, HTTPException, Request
import asyncio
import time
import os
import json

from shared.interfaces.web_crawler import (
    CrawlRequest,
    CrawlResponse,
    CrawlResult,
    SingleCrawlRequest,
    SingleCrawlResponse,
    VisionExtractRequest,
    VisionExtractResponse,
)
from shared import setup_logger, DatabaseContext, DatabaseConfig, LLMClient, MODEL_VISION
from shared.renderer_client import RendererClient
from shared.interfaces.renderer import RendererScreenshotRequest
from ..core import WebCrawlerAgent, CrawlerSettings

router = APIRouter()
logger = setup_logger("web_crawler.api.routes")


@router.post("/crawl", response_model=CrawlResponse)
async def crawl(request: CrawlRequest, http_req: Request) -> CrawlResponse:
    start_time = time.time()
    try:
        db_context = getattr(http_req.app.state, "db_context", None)
        use_database = db_context is not None
        settings = CrawlerSettings(
            max_pages=request.max_pages,
            max_depth=request.max_depth,
            respect_robots=request.respect_robots,
            timeout=request.timeout,
            max_total_time=request.max_total_time,
            max_concurrent_pages=request.max_concurrent_pages,
            memory_threshold=float(os.getenv("CRAWLER_MEMORY_THRESHOLD", "80.0")),
            user_agent=os.getenv(
                "CRAWLER_USER_AGENT",
                CrawlerSettings.model_fields["user_agent"].default,
            ),
            allowed_domains=request.allowed_domains,
            exclude_patterns=request.exclude_patterns,
        )

        async with WebCrawlerAgent(settings, db_context=db_context, use_database=use_database) as agent:
            results = await agent.crawl_urls(request.urls)

        elapsed_time = time.time() - start_time
        return CrawlResponse(
            success=True,
            results=[CrawlResult(**result) for result in results],
            total_urls=len(request.urls),
            crawled_urls=len(results),
            elapsed_time=elapsed_time,
        )
    except Exception as e:
        logger.error(f"Error during crawling: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/crawl-single",
    response_model=SingleCrawlResponse,
    status_code=200,
    tags=["Single URL Crawling"],
)
async def crawl_single(request: SingleCrawlRequest, http_req: Request) -> SingleCrawlResponse:
    start_time = time.time()
    db_context = getattr(http_req.app.state, "db_context", None)
    use_database = db_context is not None

    try:
        settings = CrawlerSettings(
            max_pages=1,
            max_depth=1,
            respect_robots=False,  # Always false for single crawl
            timeout=min(request.timeout, 30000),
            max_total_time=min(300, request.timeout // 1000 + 60),
            max_concurrent_pages=1,
            memory_threshold=float(os.getenv("CRAWLER_MEMORY_THRESHOLD", "85.0")),
            user_agent=os.getenv(
                "CRAWLER_USER_AGENT",
                CrawlerSettings.model_fields["user_agent"].default,
            ),
        )

        async with WebCrawlerAgent(settings, db_context=db_context, use_database=use_database) as agent:
            try:
                result = await asyncio.wait_for(agent.crawl_url(request.url), timeout=request.timeout / 1000.0)
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=408,
                    detail=f"Request timed out after {request.timeout/1000:.1f} seconds",
                )

        elapsed_time = time.time() - start_time

        if result:
            return SingleCrawlResponse(success=True, result=CrawlResult(**result), elapsed_time=elapsed_time)
        else:
            return SingleCrawlResponse(success=False, result=None, elapsed_time=elapsed_time, error="No content could be extracted from the URL")
    except asyncio.TimeoutError:
        elapsed_time = time.time() - start_time
        raise HTTPException(status_code=408, detail=f"Request timed out after {elapsed_time:.2f} seconds")
    except Exception as e:
        elapsed_time = time.time() - start_time
        error_msg = str(e)
        if "timeout" in error_msg.lower():
            raise HTTPException(status_code=408, detail=f"Request timeout: {error_msg}")
        elif "robots.txt" in error_msg.lower() or "robot" in error_msg.lower():
            raise HTTPException(status_code=403, detail=f"Forbidden by robots.txt: {error_msg}")
        elif "invalid url" in error_msg.lower() or "url" in error_msg.lower():
            raise HTTPException(status_code=400, detail=f"Invalid URL: {error_msg}")
        elif "connection" in error_msg.lower() or "network" in error_msg.lower():
            raise HTTPException(status_code=503, detail=f"Network error: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Server error: {error_msg}")


@router.get("/health")
async def health_check(http_req: Request):
    try:
        status = {"status": "ok", "database": "unknown"}
        db_context = getattr(http_req.app.state, "db_context", None)
        if db_context:
            try:
                async with db_context.db.get_session() as session:
                    from sqlalchemy import text

                    await session.execute(text("SELECT 1"))
                status["database"] = "connected"
            except Exception:
                status["database"] = "disconnected"
                status["status"] = "degraded"
        else:
            status["database"] = "not_initialized"
            status["status"] = "degraded"
        return status
    except Exception:
        return {"status": "ok", "database": "error"}


@router.post(
    "/extract-vision",
    response_model=VisionExtractResponse,
    status_code=200,
    tags=["Vision Extraction"],
)
async def extract_vision(request: VisionExtractRequest, http_req: Request) -> VisionExtractResponse:
    start_time = time.time()
    # No db_context needed here
    viewport_width = int(os.getenv("CRAWLER_VIEWPORT_WIDTH", "1920"))
    viewport_height = int(os.getenv("CRAWLER_VIEWPORT_HEIGHT", "1080"))
    llm_base_url = os.getenv("LLM_BASE_URL", "http://home.server:30080/llm")
    llm_model = os.getenv("LLM_MODEL", MODEL_VISION)
    renderer_base_url = os.getenv("RENDERER_URL", "http://home.server:30080/renderer")

    try:
        logger.info(
            f"extract-vision:start url={request.url} timeout={request.timeout} renderer={renderer_base_url} "
            f"llm={llm_base_url} model={llm_model} vw={viewport_width} vh={viewport_height}"
        )
        async with RendererClient(base_url=renderer_base_url) as renderer:
            logger.debug("extract-vision: calling renderer.screenshot")
            shot = await renderer.screenshot(
                **RendererScreenshotRequest(
                    url=request.url,
                    wait_for_selector="body",
                    timeout_ms=request.timeout,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                        viewport_randomize=True,
                    full_page=True,
                    full_page_strategy="scroll",
                ).model_dump()
            )
            logger.debug(
                f"extract-vision: renderer response keys={list(shot.keys()) if isinstance(shot, dict) else type(shot)}"
            )
            image_b64 = shot.get("screenshot_b64")
            if not image_b64:
                logger.error("extract-vision: renderer returned no screenshot_b64")
                raise HTTPException(status_code=500, detail="Renderer did not return screenshot data")
            logger.debug(f"extract-vision: screenshot size (b64 chars)={len(image_b64)}")

        fields = request.fields or ["name", "price", "currency", "availability"]
        keys_csv = ", ".join(fields)
        instruction = (
            "You are extracting structured data from a webpage screenshot. "
            f"Return JSON only with keys: {keys_csv}. "
            "For missing values use null. Do not include extra keys or text."
        )

        logger.debug(
            f"extract-vision: calling LLM vision model={llm_model} base={llm_base_url} fields={keys_csv}"
        )
        async with LLMClient(base_url=llm_base_url, model=llm_model) as llm:
            content = await llm.extract_from_image(
                image_base64=image_b64,
                instruction=instruction,
                model=llm_model,
                format="json",
            )
        logger.debug(f"extract-vision: llm content prefix={str(content)[:200]}")

        data = None
        try:
            data = json.loads(content)
        except Exception:
            try:
                content_stripped = content.strip().strip("` ")
                if content_stripped.startswith("json"):
                    content_stripped = content_stripped[4:].strip()
                data = json.loads(content_stripped)
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(
                    f"extract-vision: JSON parse failed. prefix={str(content)[:200]} error={e}")
                return VisionExtractResponse(success=False, data=None, elapsed_time=elapsed, error=str(e))

        elapsed = time.time() - start_time
        logger.info(f"extract-vision: success in {elapsed:.2f}s for url={request.url}")
        return VisionExtractResponse(success=True, data=data, elapsed_time=elapsed)
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        logger.error(f"extract-vision: timeout after {elapsed:.2f}s url={request.url}")
        raise HTTPException(status_code=408, detail=f"Request timed out after {elapsed:.2f}s")
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"extract-vision: error {e} after {elapsed:.2f}s url={request.url}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


