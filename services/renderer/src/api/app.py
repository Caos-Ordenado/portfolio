from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
from shared import setup_logger
from .routes import router
import asyncio
import os
import time

logger = setup_logger("renderer.api")
load_dotenv()

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown hooks (replaces the deprecated @app.on_event handlers)."""
    await on_startup()
    try:
        yield
    finally:
        await on_shutdown()


app = FastAPI(lifespan=lifespan, title="Renderer Service", version="0.1.0")
app.include_router(router)


async def _cleanup_daemon():
    base_dir = os.getenv("RENDERER_SNAPSHOT_DIR", "/tmp/renderer-snapshots")
    ttl_seconds = int(os.getenv("RENDERER_SNAPSHOT_TTL_SECONDS", "86400"))
    interval = int(os.getenv("RENDERER_CLEANUP_INTERVAL_SECONDS", "3600"))
    os.makedirs(base_dir, exist_ok=True)
    logger.info(
        f"renderer cleanup: dir={base_dir}, ttl_seconds={ttl_seconds}, interval={interval}"
    )
    while True:
        try:
            threshold = time.time() - ttl_seconds
            for name in os.listdir(base_dir):
                path = os.path.join(base_dir, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < threshold:
                        os.remove(path)
                        logger.debug(f"renderer cleanup: removed {path}")
                except Exception as e:
                    logger.error(f"cleanup error for {path}: {e}")
        except Exception as e:
            logger.error(f"cleanup scan error: {e}")
        await asyncio.sleep(interval)


async def on_startup():
    app.state.cleanup_task = asyncio.create_task(_cleanup_daemon())
    if not PLAYWRIGHT_AVAILABLE:
        return
    headless = os.getenv("RENDERER_HEADLESS", "true").lower() == "true"
    try:
        app.state.pw = await async_playwright().start()
        app.state.browser = await app.state.pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        logger.info("Playwright browser launched")
    except Exception as e:
        logger.error(f"startup launch error: {e}")
        app.state.browser = None


async def on_shutdown():
    try:
        if getattr(app.state, "browser", None):
            await app.state.browser.close()
        if getattr(app.state, "pw", None):
            await app.state.pw.stop()
    except Exception:
        pass
    try:
        if getattr(app.state, "cleanup_task", None):
            app.state.cleanup_task.cancel()
    except Exception:
        pass



