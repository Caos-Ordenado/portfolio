from shared.logging import setup_logger
from src.api.models import BraveSearchResult, ExtractedUrlInfo, IdentifiedPageCandidate, ProductWithPrice
from .query_generator import QueryGeneratorAgent
from .search_agent import SearchAgent
from .query_validator import QueryValidatorAgent
from .url_extractor_agent import UrlExtractorAgent
from .product_page_candidate_identifier import ProductPageCandidateIdentifierAgent
from .price_extractor import PriceExtractorAgent
from .relevance_scorer import RelevanceScorer
from .geo_url_validator_agent import GeoUrlValidatorAgent
from .category_expansion_agent import CategoryExpansionAgent

# Updated service imports
from .web_crawler_trigger_service import WebCrawlerTriggerService
from .web_crawler_data_retrieval_service import WebCrawlerDataRetrievalService
from ..tools.web_crawler_data_retrieval_tool import fetch_web_crawler_data_tool, set_web_crawler_data_retrieval_dependencies
# For database session and redis client, you'll need to ixsmport your actual setup
# from shared.database.manager import DatabaseManager # Example: if you need to pass db_manager
# from shared.database.session import SessionLocal, get_db # Example
# from shared.redis_client import get_redis_client # Example

import inspect
import os

logger = setup_logger("product_search_agent")

MAX_VALIDATION_ATTEMPTS = 3
TARGET_VALID_QUERIES = 5

class ProductSearchAgent:
    def __init__(self, country: str = "UY", city: str = None): # Potentially add db_session, redis_client if not handled globally
        logger.info(f"ProductSearchAgent initialized for {country}" + (f"/{city}" if city else ""))
        self.country = country
        self.city = city
        
        self.query_generator = QueryGeneratorAgent()
        self.search_agent = SearchAgent()
        self.query_validator = QueryValidatorAgent()
        self.url_extractor = UrlExtractorAgent(llm_threshold=20)  # Enhanced with pre-filtering
        self.page_identifier = ProductPageCandidateIdentifierAgent() # Assuming no crawler tool passed now
        self.price_extractor = PriceExtractorAgent()
        self.category_expander = CategoryExpansionAgent()
        self.relevance_scorer = RelevanceScorer()
        
        # Initialize geographic URL validator
        self.url_validator = GeoUrlValidatorAgent(country=country, city=city)

        # Instantiate new services
        self.crawler_trigger_service = WebCrawlerTriggerService()
        
        # DataRetrievalService requires db_session and redis_client (via WebPageRepository which needs DatabaseManager)
        # You need to manage how these are provided. For example, if using FastAPI an app dependency:
        # db_manager = DatabaseManager()
        # await db_manager.init() # Initialize it
        # webpage_repo = WebPageRepository(db_manager)
        # self.data_retrieval_service = WebCrawlerDataRetrievalService(repository=webpage_repo)
        # set_web_crawler_data_retrieval_dependencies(service=self.data_retrieval_service, db_manager=db_manager)
        # For now, we'll assume they are None and it will log warnings.
        # In a real app, these MUST be provided.
        # Consider making __init__ async if session/client retrieval is async.
        # Or pass them in if ProductSearchAgent is created where sessions are available.
        # For the tool to work, set_web_crawler_data_retrieval_dependencies MUST be called.
        # This setup is simplified for this example.
        # You should integrate this with your FastAPI dependency injection or session management.
        
        # Placeholder: Initialize db_manager, webpage_repo and data_retrieval_service according to your application's structure
        # Example for illustration (actual implementation depends on your app's context):
        # async def initialize_agent_dependencies(): # Example async init helper
        #     db_manager = DatabaseManager()
        #     await db_manager.init() # Assuming DatabaseConfig is from env or default
        #     webpage_repo = WebPageRepository(db_manager)
        #     self.data_retrieval_service = WebCrawlerDataRetrievalService(repository=webpage_repo)
        #     set_web_crawler_data_retrieval_dependencies(service=self.data_retrieval_service, db_manager=db_manager)
        #     logger.info("DataRetrievalService and its dependencies initialized for ProductSearchAgent.")
        # asyncio.run(initialize_agent_dependencies()) # Or call appropriately if in an async context

        logger.warning(
            "DataRetrievalService may not be fully initialized here. "
            "Ensure DatabaseManager is initialized, WebPageRepository and WebCrawlerDataRetrievalService are instantiated, "
            "and set_web_crawler_data_retrieval_dependencies is called with both the service and db_manager."
        )
        
        # Which of your sub-agents is the Langchain agent that will use fetch_web_crawler_data_tool?
        # Let's assume QueryGeneratorAgent for now, as an example.
        # You would add `fetch_web_crawler_data_tool` to its list of tools.
        # e.g., self.query_generator = QueryGeneratorAgent(tools=[..., fetch_web_crawler_data_tool])

    async def __aenter__(self):
        logger.debug("Entering ProductSearchAgent context")
        await self.query_generator.__aenter__()
        await self.search_agent.__aenter__()
        await self.query_validator.__aenter__()
        if self.url_extractor and hasattr(self.url_extractor, '__aenter__') and callable(getattr(self.url_extractor, '__aenter__')):
            await self.url_extractor.__aenter__()
        else:
            logger.error("Skipping await self.url_extractor.__aenter__() because it is missing or not callable at __aenter__ call time.")
        await self.page_identifier.__aenter__()
        await self.price_extractor.__aenter__()
        # URL validator doesn't need async context management since it's stateless
        # No specific async context needed for CrawlerTriggerService or DataRetrievalService if their methods are stateless calls
        # or if their dependencies (like DB sessions) are managed per call or externally.
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("Exiting ProductSearchAgent context")
        await self.query_generator.__aexit__(exc_type, exc_val, exc_tb)
        await self.search_agent.__aexit__(exc_type, exc_val, exc_tb)
        await self.query_validator.__aexit__(exc_type, exc_val, exc_tb)
        if self.url_extractor and hasattr(self.url_extractor, '__aexit__') and callable(getattr(self.url_extractor, '__aexit__')):
            await self.url_extractor.__aexit__(exc_type, exc_val, exc_tb)
        else:
            logger.error("Skipping await self.url_extractor.__aexit__() because it is missing or not callable at __aexit__ call time.")
        await self.page_identifier.__aexit__(exc_type, exc_val, exc_tb)
        await self.price_extractor.__aexit__(exc_type, exc_val, exc_tb)
        # URL validator doesn't need async context management since it's stateless
        pass


    async def search_product(self, product: str):
        logger.info(f"Original product search query: {product}")
        
        validation_attempts_count = 0
        valid_queries = [] 
        extracted_candidates_list = [] # List[ExtractedUrlInfo]
        identified_page_candidates_list = [] # List[IdentifiedPageCandidate]

        # ... (existing query generation and validation loop should be here) ...
        # This part of the code was missing in the previous view, assuming it populates valid_queries
        # For example:
        # for attempt in range(MAX_VALIDATION_ATTEMPTS):
        #     ... (generate and validate queries) ...
        #     if len(valid_queries) >= TARGET_VALID_QUERIES: break
        # else:
        #     ... (log if not enough queries) ...
        # For brevity, I'm skipping the full loop, ensure it's present in your actual code.
        # This is a simplified placeholder for query generation:
        generated_queries_response = await self.query_generator.generate_queries(product)
        if generated_queries_response and generated_queries_response[0]:
            valid_queries = generated_queries_response[0] # Assuming it returns a list in the first element
        else:
            valid_queries = [product] # Fallback to original product query
        logger.info(f"Using queries for search: {valid_queries}")


        if not valid_queries:
            logger.error("No valid queries found after all attempts.")
            return (
                ["[No valid queries found]"], 
                validation_attempts_count,
                [],
                []
            )
        
        brave_search_results_internal = await self.search_agent.aggregate_search(valid_queries)
        
        if self.url_extractor:
            extracted_candidates_list = await self.url_extractor.extract_product_url_info(brave_search_results_internal)
        else:
            logger.error("self.url_extractor is None at the time of calling extract_product_url_info.")
            extracted_candidates_list = []
        
        # Phase 2.5: Geographic URL Validation
        if extracted_candidates_list and self.url_validator:
            try:
                # Extract URLs from extracted candidates for validation
                urls_to_validate = [candidate.url for candidate in extracted_candidates_list if candidate.url]
                logger.info(f"Validating {len(urls_to_validate)} URLs for {self.country}" + (f"/{self.city}" if self.city else ""))
                
                # Get the first valid query for context
                search_query = valid_queries[0] if valid_queries else product
                validated_urls = await self.url_validator.validate_urls(urls_to_validate, search_query)
                
                # Filter extracted_candidates_list to only include validated URLs
                if validated_urls:
                    filtered_candidates_list = []
                    for candidate in extracted_candidates_list:
                        if candidate.url in validated_urls:
                            filtered_candidates_list.append(candidate)
                    
                    logger.info(f"URL validation reduced candidates from {len(extracted_candidates_list)} to {len(filtered_candidates_list)}")
                    extracted_candidates_list = filtered_candidates_list
                else:
                    logger.warning(f"No {self.country}-relevant URLs found, filtering all candidates")
                    extracted_candidates_list = []
                    
            except Exception as e:
                logger.error(f"URL validation failed: {e}. Filtering all candidates")
                extracted_candidates_list = []
        
        # Placeholder: Where to use CrawlerTriggerService?
        # Option 1: Trigger crawls for all extracted URLs proactively
        if extracted_candidates_list and self.crawler_trigger_service:
            urls_to_potentially_crawl = [candidate.url for candidate in extracted_candidates_list if candidate.url]
            if urls_to_potentially_crawl:
                logger.info(f"Proactively triggering crawls for {len(urls_to_potentially_crawl)} URLs.")
                # This is a fire-and-forget trigger; we don't wait for crawl completion here.
                # The web_crawler_service will save data to DB/Redis.
                # Consider batching or limiting concurrent triggers if many URLs.
                await self.crawler_trigger_service.trigger_crawls(urls_to_crawl=urls_to_potentially_crawl)
        
        # The DataRetrievalService (and its tool fetch_web_crawler_data_tool) would be used by an LLM-based sub-agent.
        # For example, ProductPageCandidateIdentifierAgent or a new "ContentAnalysisAgent".
        # That agent, when processing a URL, would decide if it needs to fetch full content via the tool.
        # Example: if identified_page_candidates_list were processed by another LLM agent:
        # for candidate_page in identified_page_candidates_list:
        #     # An LLM agent might decide to call fetch_web_crawler_data_tool for candidate_page.url here
        #     # if it needs more content than the snippet.
        #     pass

        if extracted_candidates_list:
            # ProductPageCandidateIdentifierAgent currently uses snippets.
            # If it were an LLM agent with fetch_web_crawler_data_tool, it could fetch full content.
            identified_page_candidates_list = await self.page_identifier.identify_batch_page_types(
                extracted_candidates_list, product
            )

        # Phase 2.7: Expand category pages into likely product pages and re-validate
        if identified_page_candidates_list:
            # Keep only relevant candidate types from identification step
            identified_page_candidates_list = [
                c for c in identified_page_candidates_list
                if getattr(c, 'page_type', None) in ('PRODUCT', 'CATEGORY')
            ]

            category_urls = [c.url for c in identified_page_candidates_list if getattr(c, 'page_type', None) == 'CATEGORY']
            if category_urls:
                # Use the original product search query for dynamic filtering
                query_terms = product.split() if product else None
                expanded = await self.category_expander.expand(category_urls, query_terms)
                if expanded:
                    try:
                        search_query = valid_queries[0] if valid_queries else product
                        expanded_valid = await self.url_validator.validate_urls(expanded, search_query)
                    except Exception as e:
                        logger.error(f"Expanded URL validation failed: {e}. Dropping expanded URLs")
                        expanded_valid = []

                    # Preserve existing PRODUCT candidates as-is
                    preserved_products: list[IdentifiedPageCandidate] = [
                        c for c in identified_page_candidates_list if getattr(c, 'page_type', None) == 'PRODUCT'
                    ]

                    # Re-classify expanded URLs (no prompt changes) and keep only PRODUCT.
                    existing_urls = {c.url for c in preserved_products}
                    expanded_to_classify = [u for u in expanded_valid if u not in existing_urls]
                    if expanded_to_classify:
                        expanded_url_infos: list[ExtractedUrlInfo] = [
                            ExtractedUrlInfo(
                                url=u,
                                original_title="",
                                original_snippet="",
                                source_query=product,
                            )
                            for u in expanded_to_classify
                        ]
                        reclassified = await self.page_identifier.identify_batch_page_types(
                            expanded_url_infos, product
                        )
                        reclassified_products = [
                            c for c in reclassified if getattr(c, "page_type", None) == "PRODUCT"
                        ]
                        preserved_products.extend(reclassified_products)

                    identified_page_candidates_list = preserved_products
        
        # Relevance scoring + Montevideo preference ranking (Uruguay-only already enforced upstream)
        if identified_page_candidates_list:
            max_candidates = int(os.getenv("MAX_PRODUCT_CANDIDATES_FOR_PRICE", "10"))
            min_relevance = float(os.getenv("MIN_RELEVANCE_SCORE", "0.2"))
            scored_candidates = []
            for candidate in identified_page_candidates_list:
                scores = await self.relevance_scorer.score_candidate(
                    product_query=product,
                    url=candidate.url,
                    title=candidate.original_title,
                    snippet=candidate.original_snippet,
                )
                scored_candidates.append(candidate.model_copy(update=scores))

            scored_candidates.sort(key=lambda c: c.combined_score or 0.0, reverse=True)
            filtered_candidates = [
                c for c in scored_candidates
                if (c.relevance_score or 0.0) >= min_relevance
            ]
            if not filtered_candidates:
                filtered_candidates = scored_candidates
                logger.info(
                    "Relevance gate kept no candidates above threshold; "
                    f"falling back to top-ranked list (cap={max_candidates})."
                )
            identified_page_candidates_list = filtered_candidates[:max_candidates]
            logger.info(
                "Relevance gate kept top %d candidates (cap=%d, min_relevance=%.2f)",
                len(identified_page_candidates_list),
                max_candidates,
                min_relevance,
            )

        logger.debug(f"Returning from search_product. Type of identified_page_candidates_list: {type(identified_page_candidates_list)}")
        if isinstance(identified_page_candidates_list, list):
            logger.debug(f"Number of items in identified_page_candidates_list: {len(identified_page_candidates_list)}")
            for i, item in enumerate(identified_page_candidates_list):
                logger.debug(f"Item {i} in identified_page_candidates_list - Type: {type(item)}")
                if not isinstance(item, IdentifiedPageCandidate):
                    logger.error(f"Item {i} is NOT an IdentifiedPageCandidate instance! Content: {item}")
        else:
            logger.error(f"identified_page_candidates_list is NOT a list! Content: {identified_page_candidates_list}")

        # Phase 3: Price Extraction from PRODUCT pages
        extracted_prices = []
        if identified_page_candidates_list:
            try:
                logger.info("Starting price extraction from identified product pages")
                extracted_prices = await self.price_extractor.extract_prices(identified_page_candidates_list)
                logger.info(f"Price extraction complete: {len(extracted_prices)} products processed")
            except Exception as e:
                logger.error(f"Price extraction failed: {e}", exc_info=True)
                # Continue without prices rather than failing completely
                extracted_prices = []

        return (
            valid_queries, 
            validation_attempts_count,
            extracted_candidates_list, 
            identified_page_candidates_list,
            extracted_prices  # 🆕 NEW: Products with price information, sorted by price
        ) 