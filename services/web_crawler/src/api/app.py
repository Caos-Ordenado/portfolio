from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from shared.logging import setup_logger
from shared import DatabaseContext, DatabaseConfig
from dotenv import load_dotenv
import asyncio
import os
from .routes import router

logger = setup_logger("web_crawler.api")
load_dotenv()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown hooks (replaces the deprecated @app.on_event handlers)."""
    await startup_event()
    try:
        yield
    finally:
        await shutdown_event()


app = FastAPI(
    lifespan=lifespan,
    title="Web Crawler API",
    description="A high-performance web crawler with memory-adaptive features.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


async def cleanup_task():
    cleanup_interval = int(os.getenv("CRAWLER_CLEANUP_INTERVAL_HOURS", "24"))
    retention_days = int(os.getenv("CRAWLER_DATA_RETENTION_DAYS", "30"))
    while True:
        try:
            if getattr(app.state, "db_context", None):
                async with app.state.db_context.db.get_session() as session:
                    count = await app.state.db_context.webpages.cleanup_old_pages(session, days=retention_days)
                    logger.info(f"Cleaned up {count} old pages")
            await asyncio.sleep(cleanup_interval * 3600)
        except Exception as e:
            logger.error(f"Error during cleanup task: {e}")
            await asyncio.sleep(3600)


async def startup_event():
    logger.info("Initializing web crawler API...")
    max_retries = 5
    retry_delay = 2
    for attempt in range(max_retries):
        try:
            app.state.db_context = DatabaseContext(
                config=DatabaseConfig(
                    postgres_host=os.getenv("POSTGRES_HOST", "home.server"),
                    postgres_port=int(os.getenv("POSTGRES_PORT", "5432")),
                    postgres_db=os.getenv("POSTGRES_DB", "web_crawler"),
                    postgres_user=os.getenv("POSTGRES_USER", "admin"),
                    postgres_password=os.getenv("POSTGRES_PASSWORD"),
                    redis_host=os.getenv("REDIS_HOST", "home.server"),
                    redis_port=int(os.getenv("REDIS_PORT", "6379")),
                    redis_db=int(os.getenv("REDIS_DB", "0")),
                    redis_password=os.getenv("REDIS_PASSWORD"),
                )
            )
            await app.state.db_context.__aenter__()
            logger.info("Database context initialized successfully")
            break
        except Exception as e:
            logger.warning(f"Database initialization attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.error("Failed to initialize database context after all retries")
                app.state.db_context = None
    if getattr(app.state, "db_context", None):
        asyncio.create_task(cleanup_task())
    else:
        logger.warning("Cleanup task disabled due to database initialization failure")
    logger.info("Web crawler API initialization complete")


async def shutdown_event():
    if getattr(app.state, "db_context", None):
        await app.state.db_context.__aexit__(None, None, None)
        app.state.db_context = None
        logger.info("Database connections closed")