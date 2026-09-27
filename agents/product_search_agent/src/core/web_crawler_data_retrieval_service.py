from typing import Optional, List, Any, Dict
from shared.logging import setup_logger
from sqlalchemy.ext.asyncio import AsyncSession # For type hinting, used by shared repo

# Use the shared repository and model
from shared.repositories.webpage import WebPageRepository
from shared.models.webpage import WebPage # The SQLAlchemy model

# We'll define a Pydantic model for the tool's output, mapping from WebPage
from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field

logger = setup_logger("product_search_agent.web_crawler_data_retrieval_service") # Renamed logger

class RetrievedPageData(PydanticBaseModel):
    """Pydantic model for data retrieved by WebCrawlerDataRetrievalService, based on shared.models.WebPage.""" # Renamed in docstring
    url: str
    title: Optional[str] = None
    status_code: Optional[int] = None
    content_type: Optional[str] = None
    text_content: Optional[str] = Field(None, alias="full_text") # Or use main_content
    description: Optional[str] = None
    links: Optional[List[str]] = None # Assuming links in WebPage are List[str]
    meta_tags: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, Any]] = None
    images: Optional[List[Dict[str, Any]]] = None
    structured_data: Optional[Dict[str, Any]] = None
    content_language: Optional[str] = None
    crawled_at: Optional[str] = None
    last_modified: Optional[str] = None

    # from_attributes: build from the SQLAlchemy WebPage; populate_by_name: allow alias "full_text"
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @classmethod
    def from_shared_webpage(cls, webpage: WebPage) -> "RetrievedPageData":
        # Custom mapping if needed, or rely on Pydantic's from_orm if field names match
        return cls(
            url=webpage.url,
            title=webpage.title,
            status_code=webpage.status_code,
            content_type=webpage.content_type,
            text_content=webpage.full_text or webpage.main_content, # Prioritize full_text
            description=webpage.description,
            links=webpage.links if isinstance(webpage.links, list) else None, # Ensure it's a list
            meta_tags=webpage.meta_tags if isinstance(webpage.meta_tags, dict) else None,
            headers=webpage.headers if isinstance(webpage.headers, dict) else None,
            images=webpage.images if isinstance(webpage.images, list) else None,
            structured_data=webpage.structured_data if isinstance(webpage.structured_data, dict) else None,
            content_language=webpage.content_language,
            crawled_at=webpage.crawled_at.isoformat() if webpage.crawled_at else None,
            last_modified=webpage.last_modified.isoformat() if webpage.last_modified else None
        )


class WebCrawlerDataRetrievalService: # Renamed class
    def __init__(self, repository: WebPageRepository): # Pass the repository instance
        """
        Initializes the WebCrawlerDataRetrievalService.
        Args:
            repository: An instance of the shared WebPageRepository.
        """
        self.repository = repository
        logger.info("WebCrawlerDataRetrievalService initialized with shared WebPageRepository.") # Renamed in log string

    async def get_crawled_data_for_url(self, session: AsyncSession, url: str) -> Optional[RetrievedPageData]:
        """
        Retrieves crawled data for a single URL using the shared WebPageRepository.
        The session is passed here because the shared repository methods require it.
        """
        if not url:
            logger.warning("No URL provided to get_crawled_data_for_url.")
            return None
        
        logger.info(f"Attempting to retrieve crawled data for URL: {url}")
        try:
            # The shared repository's get_by_url method requires an AsyncSession
            webpage_instance: Optional[WebPage] = await self.repository.get_by_url(session, url)
            
            if webpage_instance:
                logger.info(f"Successfully retrieved data for URL: {url} from repository.")
                return RetrievedPageData.from_shared_webpage(webpage_instance)
            else:
                logger.info(f"No data found for URL: {url} in repository.")
                return None
        except Exception as e:
            logger.error(f"Error retrieving data for URL {url} from repository: {e}", exc_info=True)
            return None

    async def get_batch_crawled_data(self, session: AsyncSession, urls: List[str]) -> List[Optional[RetrievedPageData]]:
        """
        Retrieves crawled data for a batch of URLs.
        """
        if not urls:
            logger.warning("No URLs provided to get_batch_crawled_data.")
            return []
        
        logger.info(f"Attempting to retrieve crawled data for {len(urls)} URLs, e.g., {urls[:3]}")
        results: List[Optional[RetrievedPageData]] = []
        try:
            # Naive batch: iterate and call single fetch. Shared repo doesn't have a bulk get_by_urls.
            # For true batch performance, one might be added to WebPageRepository.
            for url in urls:
                data = await self.get_crawled_data_for_url(session, url)
                results.append(data)
            
            successful_retrievals = sum(1 for item in results if item is not None)
            logger.info(f"Retrieved data for {successful_retrievals}/{len(urls)} URLs.")
            return results
        except Exception as e:
            logger.error(f"Error retrieving batch data for URLs from repository: {e}", exc_info=True)
            return [None] * len(urls) 