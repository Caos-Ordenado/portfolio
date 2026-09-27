from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from sqlalchemy.ext.asyncio import AsyncSession # For the session
from shared.logging import setup_logger
# Assuming WebCrawlerDataRetrievalService is in core.web_crawler_data_retrieval_service
from ..core.web_crawler_data_retrieval_service import WebCrawlerDataRetrievalService, RetrievedPageData
# Import your actual session management context/getter
from shared.database.manager import DatabaseManager # For obtaining AsyncSession

logger = setup_logger("product_search_agent.web_crawler_data_retrieval_tool")

_data_retrieval_service_instance: Optional[WebCrawlerDataRetrievalService] = None
_db_manager_instance: Optional[DatabaseManager] = None # Global for DatabaseManager

def set_web_crawler_data_retrieval_dependencies(
    service: WebCrawlerDataRetrievalService,
    db_manager: DatabaseManager # Add db_manager dependency
):
    """Sets the necessary dependencies (service and db_manager) for the tool's function."""
    global _data_retrieval_service_instance, _db_manager_instance
    _data_retrieval_service_instance = service
    _db_manager_instance = db_manager
    logger.info("Dependencies for WebCrawlerDataRetrievalTool (service and db_manager) set.")

class WebCrawlerDataRetrievalInput(BaseModel):
    url: str = Field(..., description="The URL for which to fetch crawled data.")

async def fetch_web_crawler_data_func(url: str) -> Dict[str, Any]:
    """
    Fetches previously crawled data for a specific URL from the system's database.
    Requires WebCrawlerDataRetrievalService and DatabaseManager to be set via dependencies.
    """
    if not _data_retrieval_service_instance:
        logger.error(f"WebCrawlerDataRetrievalService instance not set for {fetch_web_crawler_data_func.__name__}.")
        return {"status": "error", "message": "WebCrawlerDataRetrievalService not configured for the tool."}
    
    if not _db_manager_instance:
        logger.error(f"DatabaseManager instance not set for {fetch_web_crawler_data_func.__name__}.")
        return {"status": "error", "message": "DatabaseManager not configured for the tool."}

    try:
        async with _db_manager_instance.get_session() as session: # Obtain session from DatabaseManager
            logger.info(f"Tool invoked: {fetch_web_crawler_data_func.__name__} for URL: {url} with active session.")
            crawled_data: Optional[RetrievedPageData] = await _data_retrieval_service_instance.get_crawled_data_for_url(session, url)

            if crawled_data:
                return {
                    "status": "success",
                    "url": url,
                    "data_found": True,
                    "content": crawled_data.dict(by_alias=True) # Use by_alias=True for Pydantic aliases
                }
            else:
                return {
                    "status": "not_found", 
                    "url": url,
                    "data_found": False,
                    "message": "No crawled data found for this URL in the database."
                }
    except Exception as e:
        logger.error(f"Error during {fetch_web_crawler_data_func.__name__} for URL {url}: {e}", exc_info=True)
        return {"status": "error", "message": f"An error occurred while fetching data: {str(e)}"}

fetch_web_crawler_data_tool = StructuredTool.from_function(
    func=fetch_web_crawler_data_func,
    name="FetchStoredWebCrawlerPageData", 
    description=(
        "Retrieves detailed, previously stored content for a specific URL from the system's "
        "internal database. Use this to access the full text, title, description, links, and "
        "other metadata of a webpage that is known or expected to have been processed and stored by the web crawler."
    ),
    args_schema=WebCrawlerDataRetrievalInput,
)

# To use this tool:
# 1. During application/agent setup (e.g., FastAPI startup or ProductSearchAgent init):
#    a. Initialize your DatabaseManager:
#       `db_manager = DatabaseManager()`
#       `await db_manager.init()` (pass config if not using env vars, pass redis_client if needed by manager)
#    b. Initialize your shared WebPageRepository (it needs the DatabaseManager):
#       `webpage_repo = WebPageRepository(db_manager=db_manager)`
#    c. Create an instance of WebCrawlerDataRetrievalService:
#       `ret_service = WebCrawlerDataRetrievalService(repository=webpage_repo)`
#    d. Call `set_web_crawler_data_retrieval_dependencies`:
#       `set_web_crawler_data_retrieval_dependencies(service=ret_service, db_manager=db_manager)`
# 2. Add `fetch_web_crawler_data_tool` to your Langchain agent's tool list.
#
# Critical: The DatabaseManager must be initialized before this tool (or the service it depends on)
# can be used, as it provides the necessary database sessions.

# Renamed variables:
# - logger
# - _data_retrieval_service_instance
# - set_web_crawler_data_retrieval_dependencies
# - WebCrawlerDataRetrievalInput
# - fetch_web_crawler_data_func
# - fetch_web_crawler_data_tool 