import asyncio
import json
import os
import re
import time
import aiohttp
import hashlib
import difflib
from typing import List, Optional, Dict, Any, Union
from urllib.parse import urlparse
from shared.logging import setup_logger
from shared.ollama_client import OllamaClient
from shared.web_crawler_client import WebCrawlerClient
from shared.renderer_client import RendererClient
from shared.redis_client import RedisClient
from src.api.models import IdentifiedPageCandidate, ProductWithPrice, PriceExtractionResult
from .batch_content_retriever import BatchContentRetriever, PageContent

logger = setup_logger("price_extractor_agent")

class PriceExtractorAgent:
    def __init__(self, model_name: str = "qwen2.5:7b", temperature: float = 0.0):
        """
        Initialize PriceExtractorAgent with LLM-based price extraction and batch content retrieval.
        
        Args:
            model_name: Ollama model to use for price extraction
            temperature: Temperature for LLM generation (0.0 for deterministic)
        """
        self.model_name = model_name
        self.temperature = temperature
        self.batch_retriever = BatchContentRetriever()
        self.price_cache_enabled = os.getenv("PRICE_CACHE_ENABLED", "true").lower() == "true"
        self.price_cache_ttl_seconds = int(os.getenv("PRICE_CACHE_TTL_SECONDS", "7200"))  # 2 hours
        logger.info(f"PriceExtractorAgent initialized with model: {model_name}, temp: {temperature}, batch retrieval enabled")

    async def __aenter__(self):
        logger.debug("Entering PriceExtractorAgent context")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("Exiting PriceExtractorAgent context")

    def _extract_from_structured_data(self, page_content: PageContent, url: str) -> Optional[PriceExtractionResult]:
        """
        Extract price from schema.org structured data or meta tags - no LLM needed.
        
        This is the fastest extraction method as it uses machine-readable data
        that e-commerce sites embed for SEO purposes.
        
        Args:
            page_content: PageContent with text, meta_tags, and structured_data
            url: URL for logging
            
        Returns:
            PriceExtractionResult if price found, None otherwise
        """
        # Try schema.org offers (JSON-LD structured data)
        if page_content.structured_data:
            for item in page_content.structured_data:
                if not isinstance(item, dict):
                    continue
                    
                # Handle Product type with offers
                if item.get("@type") == "Product" and "offers" in item:
                    offers = item["offers"]
                    price = None
                    currency = "UYU"
                    
                    if isinstance(offers, dict):
                        # Single offer or AggregateOffer
                        price = offers.get("lowPrice") or offers.get("price")
                        currency = offers.get("priceCurrency", "UYU")
                        
                        # Check nested offers array
                        if not price and "offers" in offers:
                            nested_offers = offers["offers"]
                            if isinstance(nested_offers, list) and nested_offers:
                                first_offer = nested_offers[0]
                                if isinstance(first_offer, dict):
                                    price = first_offer.get("price")
                                    currency = first_offer.get("priceCurrency", currency)
                    
                    elif isinstance(offers, list) and offers:
                        # Array of offers - take first
                        first_offer = offers[0]
                        if isinstance(first_offer, dict):
                            price = first_offer.get("price")
                            currency = first_offer.get("priceCurrency", "UYU")
                    
                    if price is not None:
                        try:
                            price_float = float(price)
                            if price_float > 0:
                                logger.info(f"Structured data extraction succeeded for {url}: {currency} {price_float}")
                                return PriceExtractionResult(
                                    success=True,
                                    price=price_float,
                                    currency=currency,
                                    original_text=f"{currency} {price}",
                                    confidence=1.0
                                )
                        except (ValueError, TypeError):
                            pass
        
        # Try meta tags (Open Graph product tags)
        if page_content.meta_tags:
            price_str = page_content.meta_tags.get("product:price:amount")
            currency = page_content.meta_tags.get("product:price:currency", "UYU")
            
            if price_str is not None:
                try:
                    price_float = float(price_str)
                    if price_float > 0:
                        logger.info(f"Meta tag extraction succeeded for {url}: {currency} {price_float}")
                        return PriceExtractionResult(
                            success=True,
                            price=price_float,
                            currency=currency,
                            original_text=f"{currency} {price_str}",
                            confidence=1.0
                        )
                except (ValueError, TypeError):
                    pass
        
        return None

    def _price_cache_key(self, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"price:{digest}"

    def _build_product_from_cache(
        self,
        page: IdentifiedPageCandidate,
        cached: Dict[str, Any],
    ) -> Optional[ProductWithPrice]:
        try:
            price_data = cached.get("price_extraction")
            if not price_data:
                return None
            price_result = PriceExtractionResult(**price_data)
            if not price_result.success:
                return None
            return ProductWithPrice(
                url=page.url,
                product_name=cached.get("product_name") or page.identified_product_name or "Unknown Product",
                original_title=page.original_title,
                source_query=page.source_query,
                price_extraction=price_result,
            )
        except Exception:
            return None

    async def _get_content_with_renderer_fallback(self, missing_urls: List[str]) -> Dict[str, PageContent]:
        """
        Fallback to renderer (Playwright) for URLs where the web crawler failed.
        
        Uses parallel processing with semaphore-limited concurrency for better performance.
        
        Args:
            missing_urls: List of URLs that couldn't be crawled
            
        Returns:
            Dict mapping URL to PageContent (text only, no metadata from renderer)
        """
        if not missing_urls:
            return {}
        
        logger.info(f"Using renderer fallback for {len(missing_urls)} URLs where crawler failed")
        
        CONCURRENT_RENDERER_LIMIT = 3  # Match Ollama concurrency
        semaphore = asyncio.Semaphore(CONCURRENT_RENDERER_LIMIT)

        def _looks_blocked_text(text: str) -> tuple[bool, str]:
            if not text:
                return True, "empty_text"
            t = text.lower()
            markers = ("captcha", "verify you are", "unusual traffic", "robot", "access denied")
            for m in markers:
                if m in t:
                    return True, f"marker:{m}"
            return False, "ok"
        
        async def render_single_url(renderer: RendererClient, url: str) -> tuple:
            """Render a single URL with semaphore-limited concurrency."""
            async with semaphore:
                try:
                    t0 = time.perf_counter()
                    result = await renderer.render_html(url=url, timeout_ms=30000, viewport_randomize=True)
                    dt_ms = int((time.perf_counter() - t0) * 1000)
                    if result and result.get("text"):
                        blocked, reason = _looks_blocked_text(result.get("text", ""))
                        logger.info(
                            f"Renderer fallback render_html: domain={urlparse(url).netloc} ms={dt_ms} "
                            f"text_chars={len(result['text'])} blocked={blocked}"
                        )
                        logger.debug(f"Renderer got content for {url} ({len(result['text'])} chars)")
                        return (url, PageContent(text=result["text"]))
                    else:
                        html_bytes = len(result.get("html", "")) if result else 0
                        logger.warning(f"Renderer returned no text for {url} (html_bytes={html_bytes})")
                        return (url, None)
                except aiohttp.ClientResponseError as e:
                    logger.warning(f"Renderer HTTP error for {url}: status={getattr(e, 'status', None)} message={e}")
                    return (url, None)
                except asyncio.TimeoutError:
                    logger.warning(f"Renderer timeout for {url}")
                    return (url, None)
                except Exception as e:
                    logger.warning(f"Renderer failed for {url}: {e}")
                    return (url, None)
        
        content_map: Dict[str, PageContent] = {}
        try:
            renderer_url = os.getenv("RENDERER_URL", "http://home.server:30080/renderer")
            async with RendererClient(base_url=renderer_url) as renderer:
                # 🚀 OPTIMIZATION: Run all renders concurrently with semaphore limiting
                results = await asyncio.gather(
                    *[render_single_url(renderer, url) for url in missing_urls],
                    return_exceptions=True
                )
                
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(f"Renderer task exception: {result}")
                    elif result[1] is not None:
                        content_map[result[0]] = result[1]
        except Exception as e:
            logger.error(f"Renderer client initialization failed: {e}")
        
        logger.info(f"Renderer fallback recovered content for {len(content_map)}/{len(missing_urls)} URLs")
        return content_map

    async def extract_prices(self, identified_pages: List[IdentifiedPageCandidate]) -> List[ProductWithPrice]:
        """
        Extract prices from identified product pages.
        
        Args:
            identified_pages: List of identified page candidates
            
        Returns:
            List[ProductWithPrice]: Products with extracted price information, sorted by price
        """
        logger.info(f"Starting price extraction for {len(identified_pages)} page candidates")
        
        # Strictly keep PRODUCT pages; ignore CATEGORY/OTHER and non-Uruguay exclusions
        product_pages = [
            page for page in identified_pages
            if getattr(page, 'page_type', None) == "PRODUCT"
        ]
        
        logger.info(f"Filtered to {len(product_pages)} PRODUCT pages for price extraction (CATEGORY pages excluded)")
        
        if not product_pages:
            logger.warning("No PRODUCT pages found for price extraction")
            return []
        
        extracted_products = []

        redis_client = None
        if self.price_cache_enabled:
            try:
                redis_client = RedisClient()
                await redis_client.__aenter__()
                if not await redis_client.health_check():
                    await redis_client.__aexit__(None, None, None)
                    redis_client = None
            except Exception as e:
                logger.warning(f"Price cache unavailable: {e}")
                redis_client = None
        
        # 🚀 OPTIMIZATION: Batch retrieve all page content at once
        urls_to_crawl = [page.url for page in product_pages]
        logger.info(f"Batch retrieving content for {len(urls_to_crawl)} product pages")
        
        page_contents = await self.batch_retriever.get_contents_batch(urls_to_crawl)
        
        # Log cache performance
        stats = self.batch_retriever.get_stats()
        logger.info(f"Batch retrieval stats: {stats['cache_hit_rate_percent']:.1f}% cache hit rate, "
                   f"{stats['memory_hits']} memory + {stats['redis_hits']} redis + {stats['database_hits']} db hits")
        
        # Renderer fallback for URLs where crawler failed (JS-heavy sites)
        missing_content_urls = [url for url in urls_to_crawl if url not in page_contents or not page_contents.get(url)]
        if missing_content_urls:
            logger.info(f"Crawler failed to retrieve {len(missing_content_urls)}/{len(urls_to_crawl)} URLs, trying renderer fallback")
            renderer_content = await self._get_content_with_renderer_fallback(missing_content_urls)
            page_contents.update(renderer_content)
        
        # 🚀 OPTIMIZATION: Process pages concurrently with limited parallelism
        CONCURRENT_LLM_LIMIT = 3  # Limit concurrent LLM calls to avoid overloading Ollama
        semaphore = asyncio.Semaphore(CONCURRENT_LLM_LIMIT)
        
        async def process_single_page(page: IdentifiedPageCandidate) -> List[ProductWithPrice]:
            """Process a single page for price extraction with semaphore-limited concurrency and speculative screenshot prefetch."""
            async with semaphore:
                results = []
                screenshot_task = None
                allow_vision_on_no_text = os.getenv("PRICE_VISION_ON_NO_TEXT", "true").lower() == "true"
                
                try:
                    if redis_client:
                        cached_value = await redis_client.get(self._price_cache_key(page.url))
                        if cached_value:
                            try:
                                cached = json.loads(cached_value)
                                cached_product = self._build_product_from_cache(page, cached)
                                if cached_product:
                                    logger.info(f"Price cache hit for {page.url}")
                                    return [cached_product]
                            except Exception:
                                pass

                    logger.debug(f"Extracting price for: {page.url}")
                    
                    # Get content from batch results (now returns PageContent)
                    page_content = page_contents.get(page.url)
                    if not page_content:
                        logger.warning(f"Could not retrieve content for {page.url} - attempting vision fallback")
                        if allow_vision_on_no_text:
                            vision_data = await self._extract_with_vision(page.url)
                            vision_product = self._build_product_from_vision(page, vision_data) if vision_data else None
                            if vision_product:
                                return [vision_product]
                        return [ProductWithPrice(
                            url=page.url,
                            product_name=page.identified_product_name or "Unknown Product",
                            original_title=page.original_title,
                            source_query=page.source_query,
                            price_extraction=PriceExtractionResult(
                                success=False,
                                error="Failed to retrieve page content from batch"
                            )
                        )]
                    
                    # Get text content from PageContent
                    text_content = page_content.text if isinstance(page_content, PageContent) else page_content
                        
                    # Skip pages with insufficient content (likely loading issues)  
                    if len(text_content.strip()) < 50:
                        logger.warning(f"Insufficient content for {page.url} ({len(text_content)} chars) - attempting vision fallback")
                        if allow_vision_on_no_text:
                            vision_data = await self._extract_with_vision(page.url)
                            vision_product = self._build_product_from_vision(page, vision_data) if vision_data else None
                            if vision_product:
                                return [vision_product]
                        return [ProductWithPrice(
                            url=page.url,
                            product_name=page.identified_product_name or "Unknown Product",
                            original_title=page.original_title,
                            source_query=page.source_query,
                            price_extraction=PriceExtractionResult(
                                success=False,
                                error=f"Insufficient page content ({len(text_content)} chars)"
                            )
                        )]
                    
                    # 🚀 OPTIMIZATION: Start screenshot prefetch speculatively
                    # This runs in background while text extraction runs - if text succeeds, we cancel it
                    should_prefetch = (
                        page.page_type == "PRODUCT" and
                        "rappi.com.uy" not in page.url and
                        "evisos.com.uy" not in page.url and
                        "wikipedia.org" not in page.url and
                        "acg.com.uy" not in page.url
                    )
                    if should_prefetch:
                        screenshot_task = asyncio.create_task(
                            self._prefetch_screenshot(page.url),
                            name=f"screenshot_prefetch_{page.url[:50]}"
                        )
                    
                    # Extract price using LLM as single product (catalog handled upstream)
                    products_from_page = await self._extract_products_with_llm(
                        page_content=page_content,
                        url=page.url,
                        product_name=page.identified_product_name or "unknown product",
                        page_type="PRODUCT"
                    )

                    # Vision fallback: if nothing found or low-confidence, try rendered screenshot extraction
                    need_vision = False
                    try:
                        if not products_from_page:
                            need_vision = True
                        else:
                            # if single product result with low confidence or missing price
                            if len(products_from_page) == 1:
                                pr = products_from_page[0].get('price_extraction')
                                if not pr or not getattr(pr, 'success', False) or getattr(pr, 'confidence', 0.0) < 0.6:
                                    need_vision = True
                    except Exception:
                        need_vision = True

                    if need_vision and should_prefetch:
                        logger.info(f"Attempting vision fallback for {page.url}")
                        
                        # Use prefetched screenshot if available
                        vision_data = None
                        if screenshot_task:
                            try:
                                screenshot_b64 = await screenshot_task
                                screenshot_task = None  # Mark as consumed
                                if screenshot_b64:
                                    vision_data = await self._extract_with_vision_from_screenshot(screenshot_b64, page.url)
                            except Exception as e:
                                logger.debug(f"Prefetch screenshot failed, falling back to regular vision: {e}")
                        
                        # Fall back to regular vision extraction if prefetch failed
                        if not vision_data:
                            vision_data = await self._extract_with_vision(page.url)
                        
                        if vision_data:
                            # Build original_text from vision data for currency detection
                            vision_original_text = vision_data.get('original_text') or str(vision_data.get('price', ''))
                            vision_currency = vision_data.get('currency')
                            # Add currency prefix to original_text if available for better detection
                            if vision_currency:
                                vision_original_text = f"{vision_currency} {vision_original_text}"
                            
                            price_result = PriceExtractionResult(
                                success=True,
                                price=self._coerce_price(vision_data.get('price')),
                                currency=self._normalize_currency(vision_data.get('currency')),
                                original_text=vision_original_text,
                                confidence=0.75
                            )
                            # Apply currency correction from original text (handles U$S detection)
                            price_result = self._correct_currency_from_original_text(price_result)
                            
                            products_from_page = [{
                                'product_name': page.identified_product_name or vision_data.get('name') or 'unknown product',
                                'price_extraction': price_result
                            }]
                    else:
                        # Text extraction succeeded - cancel screenshot prefetch if running
                        if screenshot_task and not screenshot_task.done():
                            screenshot_task.cancel()
                            try:
                                await screenshot_task
                            except asyncio.CancelledError:
                                pass
                            screenshot_task = None
                    
                    # Add all extracted products from this page
                    for product_result in products_from_page:
                        results.append(ProductWithPrice(
                            url=page.url,
                            product_name=product_result.get('product_name', page.identified_product_name),
                            original_title=page.original_title,
                            source_query=page.source_query,
                            price_extraction=product_result['price_extraction']
                        ))

                    if redis_client:
                        cached_success = next(
                            (p for p in results if p.price_extraction.success),
                            None,
                        )
                        if cached_success:
                            await redis_client.set(
                                self._price_cache_key(page.url),
                                {
                                    "product_name": cached_success.product_name,
                                    "price_extraction": cached_success.price_extraction.model_dump(),
                                    "extracted_at": time.time(),
                                },
                                ex=self.price_cache_ttl_seconds,
                            )
                    
                    successful_from_page = len([p for p in products_from_page if p['price_extraction'].success])
                    logger.info(f"Extracted {successful_from_page}/{len(products_from_page)} products from {page.url}")
                    return results
                        
                except Exception as e:
                    logger.error(f"Error extracting price for {page.url}: {e}", exc_info=True)
                    return [ProductWithPrice(
                        url=page.url,
                        product_name=page.identified_product_name,
                        original_title=page.original_title,
                        source_query=page.source_query,
                        price_extraction=PriceExtractionResult(
                            success=False,
                            error=f"Extraction failed: {str(e)}"
                        )
                    )]
                
                finally:
                    # Always cancel screenshot task if still running to prevent semaphore leaks
                    if screenshot_task is not None and not screenshot_task.done():
                        screenshot_task.cancel()
                        try:
                            await screenshot_task
                        except asyncio.CancelledError:
                            pass
        
        # Process all pages concurrently (limited by semaphore)
        logger.info(f"Starting concurrent price extraction for {len(product_pages)} pages (max {CONCURRENT_LLM_LIMIT} concurrent)")
        try:
            all_results = await asyncio.gather(*[process_single_page(page) for page in product_pages])
        except asyncio.CancelledError:
            logger.warning("Price extraction cancelled during shutdown, cleaning up...")
            raise
        finally:
            if redis_client:
                await redis_client.__aexit__(None, None, None)
        
        # Flatten results from all pages
        for page_results in all_results:
            extracted_products.extend(page_results)
        
        # Filter to only successful extractions (exclude failed extractions from response)
        successful_products = [p for p in extracted_products if p.price_extraction.success]
        
        # Deduplicate by URL: keep only the first/primary product per URL
        seen_urls = set()
        deduplicated_products = []
        for product in successful_products:
            if product.url not in seen_urls:
                seen_urls.add(product.url)
                deduplicated_products.append(product)
            else:
                logger.debug(f"Removing duplicate URL from results: {product.url}")
        
        if len(deduplicated_products) < len(successful_products):
            logger.info(f"URL deduplication: {len(successful_products)} → {len(deduplicated_products)} products")

        # Similarity-based dedupe (beyond URL)
        similarity_threshold = float(os.getenv("PRODUCT_NAME_SIMILARITY_THRESHOLD", "0.85"))
        name_deduped_products: List[ProductWithPrice] = []
        normalized_names: List[str] = []

        for product in deduplicated_products:
            normalized = self._normalize_product_name(product.product_name)
            if not normalized:
                name_deduped_products.append(product)
                normalized_names.append("")
                continue

            matched_index = None
            for i, existing_norm in enumerate(normalized_names):
                if not existing_norm:
                    continue
                ratio = difflib.SequenceMatcher(None, normalized, existing_norm).ratio()
                if ratio >= similarity_threshold:
                    matched_index = i
                    break

            if matched_index is None:
                name_deduped_products.append(product)
                normalized_names.append(normalized)
                continue

            existing = name_deduped_products[matched_index]
            existing_price = existing.price_extraction.price if existing.price_extraction.success else None
            new_price = product.price_extraction.price if product.price_extraction.success else None

            if existing_price is not None and new_price is not None:
                if product.price_extraction.currency == existing.price_extraction.currency:
                    if new_price < existing_price:
                        name_deduped_products[matched_index] = product
                else:
                    if (product.price_extraction.confidence or 0) > (existing.price_extraction.confidence or 0):
                        name_deduped_products[matched_index] = product
            else:
                if (product.price_extraction.confidence or 0) > (existing.price_extraction.confidence or 0):
                    name_deduped_products[matched_index] = product

        if len(name_deduped_products) < len(deduplicated_products):
            logger.info(f"Name deduplication: {len(deduplicated_products)} → {len(name_deduped_products)} products")
        
        # Sort deduplicated products by price (cheapest first)
        sorted_products = sorted(name_deduped_products, key=lambda p: p.sort_price)
        
        successful_count = len(sorted_products)
        total_count = len(extracted_products)
        logger.info(f"Price extraction complete: {successful_count}/{total_count} successful")
        logger.info(f"Returning {successful_count} products with valid prices (filtered out {total_count - successful_count} failed extractions)")
        
        return sorted_products
    
    async def _get_page_content(self, url: str) -> Optional[str]:
        """
        Get page content using the web crawler's crawl-single endpoint.
        
        Args:
            url: URL to crawl
            
        Returns:
            Optional[str]: Page text content or None if failed
        """
        try:
            async with WebCrawlerClient() as client:
                response = await client.crawl_single(
                    url=url,
                    timeout=30000  # 30 second timeout
                )
                
                if response.success and response.result:
                    logger.debug(f"Successfully retrieved content for {url} ({len(response.result.text)} chars)")
                    return response.result.text
                else:
                    error_msg = response.error or "Unknown crawl error"
                    logger.error(f"Failed to crawl {url}: {error_msg}")
                    return None
                    
        except Exception as e:
            logger.error(f"Error crawling {url}: {e}")
            return None
    
    async def _extract_products_with_llm(self, page_content: Union[str, PageContent], url: str, product_name: str, page_type: str) -> List[Dict]:
        """
        Extract product(s) and price(s) from page content with automatic catalog detection.
        
        Extraction priority:
        1. Structured data (schema.org, meta tags) - instant, no LLM
        2. Direct regex extraction - fast, no LLM
        3. LLM catalog detection - slower
        4. LLM single product extraction - slower
        (Vision fallback is handled in process_single_page)
        
        Args:
            page_content: PageContent with text+metadata, or plain text string
            url: URL of the page
            product_name: Product name being searched for
            page_type: Type of page (PRODUCT or CATEGORY)
            
        Returns:
            List[Dict]: List of products with their price extraction results
        """
        try:
            # Force single-product mode for:
            # 1. Pages pre-classified as PRODUCT in Stage 3
            # 2. URLs with patterns that indicate single product pages
            force_single_product = (
                page_type == "PRODUCT" or
                self._is_single_product_url(url)
            )
            
            if force_single_product:
                logger.debug(f"Forcing single-product extraction for {url} (page_type={page_type})")
            
            # Extract text content from PageContent if needed
            text_content = page_content.text if isinstance(page_content, PageContent) else page_content
            
            # 🚀 OPTIMIZATION 1: Try structured data extraction first (instant, no LLM)
            if force_single_product and isinstance(page_content, PageContent):
                structured_result = self._extract_from_structured_data(page_content, url)
                if structured_result and structured_result.success:
                    return [{
                        'product_name': product_name,
                        'price_extraction': structured_result
                    }]
            
            # 🚀 OPTIMIZATION 2: Try direct regex-based extraction (faster than LLM)
            if force_single_product:
                direct_result = self._try_direct_text_extraction(text_content)
                if direct_result and direct_result.get('price_extraction'):
                    price_extraction = direct_result['price_extraction']
                    if price_extraction.success and price_extraction.confidence >= 0.9:
                        logger.info(f"Direct extraction succeeded for {url}: {price_extraction.currency} {price_extraction.price}")
                        direct_result['product_name'] = product_name
                        return [direct_result]
            
            # Enhanced LLM extraction with automatic catalog detection
            llm_response = await self._extract_with_catalog_detection(text_content, url, product_name)
            
            # Check if LLM detected multiple products (catalog page)
            if isinstance(llm_response, dict) and "products" in llm_response:
                products_list = llm_response["products"]
                
                # For PRODUCT pages, only keep the first/main product to avoid "related products" pollution
                if force_single_product and len(products_list) > 1:
                    logger.info(f"Single-product mode: keeping only main product from {len(products_list)} detected for {url}")
                    products_list = products_list[:1]  # Keep only the first (main) product
                else:
                    logger.info(f"Detected catalog page with {len(products_list)} products: {url}")
                
                products = []
                
                for product_data in products_list:
                    # Create PriceExtractionResult for each product
                    price_result = PriceExtractionResult(
                        success=True,
                        price=float(product_data.get("price", 0)),
                        currency=product_data.get("currency", "UYU"),
                        original_text=product_data.get("original_text", ""),
                        confidence=product_data.get("confidence", 0.8)
                    )
                    
                    # Apply direct price parsing fix
                    if price_result.original_text:
                        direct_parsed_price = self._parse_price_directly(price_result.original_text)
                        if direct_parsed_price is not None and direct_parsed_price != price_result.price:
                            logger.warning(f"Catalog product LLM parsing error: '{price_result.original_text}' → LLM: {price_result.price}, Direct: {direct_parsed_price}")
                            price_result.price = direct_parsed_price
                            price_result.confidence = 1.0
                    
                    # Apply currency correction based on original text
                    price_result = self._correct_currency_from_original_text(price_result)
                    
                    products.append({
                        'product_name': product_data.get("product_name", product_name),
                        'price_extraction': price_result
                    })
                
                return products
            
            else:
                # Single product response - use existing logic
                price_result = await self._extract_price_with_llm(text_content, url, product_name)
                
                # Apply direct price parsing fix
                if price_result.success and price_result.original_text and price_result.price:
                    direct_parsed_price = self._parse_price_directly(price_result.original_text)
                    if direct_parsed_price is not None and direct_parsed_price != price_result.price:
                        logger.warning(f"Single product LLM parsing error: '{price_result.original_text}' → LLM: {price_result.price}, Direct: {direct_parsed_price}")
                        price_result.price = direct_parsed_price
                        price_result.confidence = 1.0
                
                # Apply currency correction based on original text
                price_result = self._correct_currency_from_original_text(price_result)
                
                return [{
                    'product_name': product_name,
                    'price_extraction': price_result
                }]
            
        except Exception as e:
            logger.error(f"Error in _extract_products_with_llm for {url}: {e}")
            return [{
                'product_name': product_name,
                'price_extraction': PriceExtractionResult(
                    success=False,
                    error=f"Extraction failed: {str(e)}"
                )
            }]
    
    def _is_single_product_url(self, url: str) -> bool:
        """
        Detect if a URL likely points to a single product page based on URL patterns.
        
        Common e-commerce single-product URL patterns:
        - /producto/...
        - /product/...
        - /p/...
        - .../p (VTEX pattern)
        - /item/...
        - /dp/... (Amazon pattern)
        - ?id=... or ?sku=... patterns
        
        Returns:
            True if URL pattern indicates a single product page
        """
        url_lower = url.lower()
        
        # Path-based patterns for single product pages
        single_product_patterns = [
            r'/producto/',
            r'/product/',
            r'/productos/',
            r'/item/',
            r'/dp/',           # Amazon
            r'/articulo/',
            r'/sku/',
            r'/p$',            # VTEX pattern: ends with /p
            r'/p\?',           # VTEX with query params
            r'/p/',            # /p/ in path
            r'\.producto\?',   # tiendainglesa pattern
            r'-\d{5,}/p$',     # VTEX product ID pattern
        ]
        
        for pattern in single_product_patterns:
            if re.search(pattern, url_lower):
                return True
        
        # Query parameter patterns indicating specific product
        if re.search(r'[?&](id|sku|product_id|productid|item)=', url_lower):
            return True
        
        return False

    async def _prefetch_screenshot(self, url: str) -> Optional[str]:
        """
        Prefetch screenshot for potential vision extraction.
        
        This is used for speculative prefetching - we start the screenshot
        fetch in parallel with text extraction. If text extraction succeeds,
        we discard this. If it fails, the screenshot is already ready.
        
        Args:
            url: URL to screenshot
            
        Returns:
            Base64-encoded screenshot or None if failed
        """
        try:
            renderer_url = os.getenv("RENDERER_URL", "http://home.server:30080/renderer")
            async with RendererClient(base_url=renderer_url) as renderer:
                shot = await renderer.screenshot(url=url, viewport_randomize=True, timeout_ms=30000)
                screenshot_b64 = shot.get("screenshot_b64")
                if screenshot_b64:
                    logger.debug(f"Prefetched screenshot for {url}")
                    return screenshot_b64
                return None
        except asyncio.CancelledError:
            # Expected when text extraction succeeds
            raise
        except Exception as e:
            logger.debug(f"Screenshot prefetch failed for {url}: {e}")
            return None

    def _build_product_from_vision(
        self,
        page: IdentifiedPageCandidate,
        vision_data: Optional[Dict[str, Any]],
    ) -> Optional[ProductWithPrice]:
        if not vision_data:
            return None
        price_val = self._coerce_price(vision_data.get("price"))
        if price_val is None:
            return None
        vision_currency = self._normalize_currency(vision_data.get("currency"))
        vision_original_text = vision_data.get("original_text") or str(vision_data.get("price", ""))
        if vision_currency:
            vision_original_text = f"{vision_currency} {vision_original_text}"
        price_result = PriceExtractionResult(
            success=True,
            price=price_val,
            currency=vision_currency,
            original_text=vision_original_text,
            confidence=0.75,
        )
        price_result = self._correct_currency_from_original_text(price_result)
        return ProductWithPrice(
            url=page.url,
            product_name=page.identified_product_name or vision_data.get("name") or "unknown product",
            original_title=page.original_title,
            source_query=page.source_query,
            price_extraction=price_result,
        )

    async def _extract_with_vision_from_screenshot(self, screenshot_b64: str, url: str) -> Optional[Dict[str, Any]]:
        """
        Extract product info from a pre-captured screenshot using vision models.
        
        This is used when we have a prefetched screenshot and want to run vision
        extraction without taking a new screenshot.
        
        Args:
            screenshot_b64: Base64-encoded screenshot image
            url: Original page URL (for logging)
            
        Returns:
            Extracted data dict or None if failed
        """
        t_start = time.perf_counter()
        
        instruction = (
            "Extract the MAIN product price from this Uruguay e-commerce screenshot. "
            "CRITICAL CURRENCY RULES: "
            "- 'U$S' or 'US$' means US DOLLARS (USD), NOT pesos! "
            "- '$' alone (without U or US prefix) means Uruguayan pesos (UYU). "
            "Look for the largest/main price displayed near 'Precio' or the product name. "
            "IGNORE wattage (like '2700 W'), model numbers, or specifications - these are NOT prices! "
            "Return ONLY JSON: {name, price (number only), currency (USD if U$S/US$, UYU if $ alone), original_text (exact price as shown)}. "
            "If multiple products shown, extract only the main/featured product."
        )
        
        # Try moondream2 first (fast, ~1.8B params)
        result = await self._try_vision_model_with_image(screenshot_b64, url, instruction, "moondream:latest")
        if result:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(f"Vision extraction (prefetched) total: {total_ms:.0f}ms for {url} (moondream succeeded)")
            return result
        
        # Fallback to qwen2.5vl:7b (slower but more accurate, 7B params)
        logger.info(f"moondream failed, trying qwen2.5vl:7b fallback for {url}")
        result = await self._try_vision_model_with_image(screenshot_b64, url, instruction, "qwen2.5vl:7b")
        if result:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(f"Vision extraction (prefetched) total: {total_ms:.0f}ms for {url} (qwen2.5vl fallback succeeded)")
            return result
        
        total_ms = (time.perf_counter() - t_start) * 1000
        logger.warning(f"All vision models failed for prefetched screenshot of {url} (total: {total_ms:.0f}ms)")
        return None

    async def _extract_with_vision(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Extract product info from a rendered screenshot using vision models.
        
        Uses moondream2 (1.8B, fast) as primary model with qwen2.5vl:7b (7B, accurate) as fallback.
        Takes screenshot ONCE and reuses it for both models.
        """
        import time
        t_start = time.perf_counter()
        
        instruction = (
            "Extract the MAIN product price from this Uruguay e-commerce screenshot. "
            "CRITICAL CURRENCY RULES: "
            "- 'U$S' or 'US$' means US DOLLARS (USD), NOT pesos! "
            "- '$' alone (without U or US prefix) means Uruguayan pesos (UYU). "
            "Look for the largest/main price displayed near 'Precio' or the product name. "
            "IGNORE wattage (like '2700 W'), model numbers, or specifications - these are NOT prices! "
            "Return ONLY JSON: {name, price (number only), currency (USD if U$S/US$, UYU if $ alone), original_text (exact price as shown)}. "
            "If multiple products shown, extract only the main/featured product."
        )
        
        # Take screenshot ONCE
        t_renderer = time.perf_counter()
        try:
            renderer_url = os.getenv("RENDERER_URL", "http://home.server:30080/renderer")
            async with RendererClient(base_url=renderer_url) as renderer:
                shot = await renderer.screenshot(url=url, viewport_randomize=True, timeout_ms=30000)
                screenshot_b64 = shot.get("screenshot_b64")
                
                if not screenshot_b64:
                    logger.warning(f"No screenshot captured for {url}")
                    return None
        except Exception as e:
            logger.warning(f"Renderer screenshot failed for {url}: {e}")
            return None
        
        renderer_ms = (time.perf_counter() - t_renderer) * 1000
        logger.debug(f"Renderer screenshot took {renderer_ms:.0f}ms for {url}")
        
        # Try moondream2 first (fast, ~1.8B params) - reuse screenshot
        result = await self._try_vision_model_with_image(screenshot_b64, url, instruction, "moondream:latest")
        if result:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(f"Vision extraction total: {total_ms:.0f}ms for {url} (moondream succeeded)")
            return result
        
        # Fallback to qwen2.5vl:7b (slower but more accurate, 7B params) - reuse SAME screenshot
        logger.info(f"moondream failed, trying qwen2.5vl:7b fallback for {url}")
        result = await self._try_vision_model_with_image(screenshot_b64, url, instruction, "qwen2.5vl:7b")
        if result:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(f"Vision extraction total: {total_ms:.0f}ms for {url} (qwen2.5vl fallback succeeded)")
            return result
        
        total_ms = (time.perf_counter() - t_start) * 1000
        logger.warning(f"All vision models failed for {url} (total: {total_ms:.0f}ms)")
        return None
    
    async def _try_vision_model_with_image(
        self, screenshot_b64: str, url: str, instruction: str, model: str
    ) -> Optional[Dict[str, Any]]:
        """
        Try to extract product info using a specific vision model with pre-captured screenshot.
        
        Args:
            screenshot_b64: Base64-encoded screenshot image
            url: Original page URL (for logging)
            instruction: Extraction instruction for the LLM
            model: Ollama vision model to use
            
        Returns:
            Extracted data dict or None if failed
        """
        import time
        t_llm = time.perf_counter()
        
        try:
            # moondream only supports 2048 context, use smaller num_predict
            num_predict = 2048 if "moondream" in model else 4096
            
            async with OllamaClient() as llm:
                result = await llm.extract_from_image(
                    image_base64=screenshot_b64,
                    instruction=instruction,
                    model=model,
                    format="json",
                    num_predict=num_predict,
                )
                
                llm_ms = (time.perf_counter() - t_llm) * 1000
                logger.debug(f"LLM {model} inference took {llm_ms:.0f}ms for {url}")
                
                if result:
                    # Parse JSON response
                    try:
                        if isinstance(result, str):
                            data = json.loads(result)
                        else:
                            data = result
                        
                        # Sanity check for extracted prices
                        price = data.get('price')
                        if price is not None:
                            try:
                                price_float = float(price)
                                # Appliances typically cost > $10 USD or > $200 UYU
                                # Reject suspiciously low prices (likely vision hallucination)
                                if price_float < 5:
                                    logger.warning(f"Vision returned suspicious price {price_float} for {url} - rejecting")
                                    return None
                                # Reject prices that look like model numbers (very large without decimals)
                                if price_float > 5000 and price_float == int(price_float):
                                    # Check if this could be a model number (like 2700 from "2700 W")
                                    currency = data.get('currency', 'UYU')
                                    if currency == 'USD' and price_float > 1000:
                                        logger.warning(f"Vision returned suspicious USD price {price_float} for {url} - likely model number")
                                        return None
                            except (ValueError, TypeError):
                                pass
                        
                        logger.debug(f"Vision extraction succeeded with {model} for {url}")
                        return data
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse vision response as JSON for {url}: {e}")
                        return None
                
                return None
                
        except Exception as e:
            logger.warning(f"Vision extraction with {model} failed for {url}: {e}")
            return None
    
    async def _try_vision_model(self, url: str, instruction: str, model: str) -> Optional[Dict[str, Any]]:
        """
        Try to extract product info using a specific vision model.
        
        DEPRECATED: Use _try_vision_model_with_image() with pre-captured screenshot instead.
        This method is kept for backwards compatibility but takes a new screenshot each time.
        
        Args:
            url: Page URL to screenshot and analyze
            instruction: Extraction instruction for the LLM
            model: Ollama vision model to use
            
        Returns:
            Extracted data dict or None if failed
        """
        import time
        t_renderer = time.perf_counter()
        
        try:
            renderer_url = os.getenv("RENDERER_URL", "http://home.server:30080/renderer")
            async with RendererClient(base_url=renderer_url) as renderer:
                # Take screenshot with viewport randomization for anti-fingerprinting
                shot = await renderer.screenshot(url=url, viewport_randomize=True, timeout_ms=30000)
                screenshot_b64 = shot.get("screenshot_b64")
                
                if not screenshot_b64:
                    logger.warning(f"No screenshot captured for {url}")
                    return None
                
                renderer_ms = (time.perf_counter() - t_renderer) * 1000
                logger.debug(f"Renderer screenshot took {renderer_ms:.0f}ms for {url}")
                
                return await self._try_vision_model_with_image(screenshot_b64, url, instruction, model)
                
        except Exception as e:
            logger.warning(f"Vision extraction with {model} failed for {url}: {e}")
            return None

    def _coerce_price(self, price_val: Any) -> Optional[float]:
        """Coerce price from string/number to float using existing direct parser where needed."""
        if price_val is None:
            return None
        try:
            return float(price_val)
        except Exception:
            parsed = self._parse_price_directly(str(price_val))
            return parsed

    def _normalize_currency(self, currency: Optional[str]) -> Optional[str]:
        if not currency:
            return None
        c = str(currency).upper().strip()
        if c in ("UYU", "USD", "EUR", "GBP"):
            return c
        # Heuristics for Uruguayan currency variations
        if c in ("$", "UY$", "U$U", "PESOS", "PESO", "PESOS URUGUAYOS"):
            return "UYU"
        if c in ("U$S", "US$", "U$$", "DOLLARS", "DOLLAR", "DOLARES"):
            return "USD"
        return c
    
    def _detect_currency_from_text(self, original_text: str) -> Optional[str]:
        """
        Detect currency from the original price text.
        
        Uruguay-specific currency patterns:
        - U$S, US$, USD → USD (US dollars)
        - $, UY$, UYU → UYU (Uruguayan pesos)
        
        Args:
            original_text: Original price text like "U$S39,00" or "$189"
            
        Returns:
            Detected currency code or None
        """
        if not original_text:
            return None
        
        text = original_text.upper().strip()
        
        # Check for USD indicators (must check these FIRST as they contain $)
        usd_patterns = ['U$S', 'US$', 'USD', 'U$$', 'DOLARES', 'DÓLARES', 'DOLLARS']
        for pattern in usd_patterns:
            if pattern in text:
                return "USD"
        
        # Check for explicit UYU indicators
        uyu_patterns = ['UYU', 'UY$', 'PESOS']
        for pattern in uyu_patterns:
            if pattern in text:
                return "UYU"
        
        # Default: if just $ sign, assume UYU (common in Uruguay)
        if '$' in text:
            return "UYU"
        
        return None
    
    def _correct_currency_from_original_text(self, price_result: PriceExtractionResult) -> PriceExtractionResult:
        """
        Correct currency in price result based on original text analysis.
        
        This is a post-processing step to fix LLM currency detection errors.
        """
        if not price_result.success or not price_result.original_text:
            return price_result
        
        detected_currency = self._detect_currency_from_text(price_result.original_text)
        
        if detected_currency and detected_currency != price_result.currency:
            logger.info(f"Currency correction: '{price_result.original_text}' → "
                       f"LLM said {price_result.currency}, corrected to {detected_currency}")
            price_result.currency = detected_currency
        
        return price_result

    async def _extract_with_catalog_detection(self, page_content: str, url: str, product_name: str) -> dict:
        """
        Extract products with automatic catalog detection based on actual page content.
        
        Returns either single product format or multi-product catalog format.
        """
        try:
            # Create enhanced system prompt for catalog detection
            system_prompt = self._create_catalog_detection_system_prompt()
            user_prompt = self._create_catalog_detection_user_prompt(page_content, url, product_name)
            
            async with OllamaClient() as llm:
                response = await llm.generate(
                    prompt=user_prompt,
                    system=system_prompt,
                    model=self.model_name,
                    temperature=self.temperature,
                    num_predict=800,  # Allow more tokens for multi-product responses
                    format="json"
                )
                
                logger.debug(f"Catalog detection LLM response for {url}: {response[:300]}...")
                
                # Parse the response
                cleaned_response = self._clean_json_response(response)
                return json.loads(cleaned_response)
                
        except Exception as e:
            logger.error(f"Catalog detection failed for {url}: {e}")
            # Fallback to single product format
            return {"found": False}
    
    def _create_catalog_detection_system_prompt(self) -> str:
        """Create enhanced system prompt that can handle both single products and catalogs."""
        return """You are an expert at extracting product prices from Uruguay e-commerce websites.

TASK: Analyze page content and extract ALL relevant products with prices.

CONTENT-BASED DETECTION:
- If page shows MULTIPLE products with prices → Extract ALL (catalog/category page)
- If page shows ONE main product with price → Extract that one (product page)

OUTPUT FORMATS:

SINGLE PRODUCT (if only one main product found):
{
  "found": true|false,
  "price": <numeric_value>,
  "currency": "UYU"|"USD",
  "original_text": "<exact_price_text_found>",
  "confidence": <0.0_to_1.0>
}

MULTIPLE PRODUCTS (if multiple products found - CATALOG PAGE):
{
  "products": [
    {
      "product_name": "<full_product_name>",
      "price": <numeric_value>,
      "currency": "UYU"|"USD",
      "original_text": "<exact_price_text_found>", 
      "confidence": <0.0_to_1.0>
    }
  ]
}

CRITICAL PRICE PARSING RULES:
1. "$189" → price: 189.0 (one hundred eighty-nine)
2. "$13.000,00" → price: 13000.0 (thirteen thousand)
3. "$45,50" → price: 45.5 (forty-five point five)
4. NEVER perform mathematical operations
5. Extract literal numerical values only

WHAT IS NOT A PRICE - DO NOT EXTRACT THESE:
- Wattage like "2700 W" or "1500W" - this is power, NOT price!
- Model numbers like "GS1700" or "SL-PL2371FF"
- Product codes or SKUs
- Dimensions like "31 x 20 cm"
- Percentages like "25% OFF"

JSON OUTPUT REQUIREMENTS:
- Return VALID JSON only (no comments, no explanations)
- Do NOT use // comments in JSON (invalid JSON)
- Do NOT add explanatory text after commas

CURRENCY DETECTION (CRITICAL - READ CAREFULLY):
- $ alone = UYU (default in Uruguay)
- U$S = USD (US dollars - VERY COMMON in Uruguay!)
- US$ = USD
- USD = USD
- UY$ = UYU
- UYU = UYU

IMPORTANT: "U$S69" means 69 US dollars (USD), NOT pesos!

For catalog pages, extract UP TO 10 most relevant products that match the search query.
Return ONLY the JSON object - no markdown, no explanations."""

    def _create_catalog_detection_user_prompt(self, content: str, url: str, product_name: str) -> str:
        """Create user prompt for catalog detection."""
        # Use more content for catalog detection
        max_content_length = 3000  # Increased for catalog pages
        truncated_content = content[:max_content_length]
        if len(content) > max_content_length:
            truncated_content += "... [content truncated]"
        
        return f"""URL: {url}
Search Query: {product_name}

ANALYZE this page content. If it's a catalog/category page with multiple products and prices, extract ALL relevant products. If it's a single product page, extract that one product.

Page content:
{truncated_content}

Extract as JSON:"""

    async def _extract_price_with_llm(self, page_content: str, url: str, product_name: str) -> PriceExtractionResult:
        """
        Extract price from page content using LLM.
        
        Args:
            page_content: Full page text content
            url: Page URL for context
            product_name: Product name for context
            
        Returns:
            PriceExtractionResult: Structured price extraction result
        """
        try:
            # Create optimized prompt for Uruguay price extraction
            system_prompt = self._create_system_prompt()
            user_prompt = self._create_user_prompt(page_content, url, product_name)
            
            # Use Ollama client for LLM inference
            async with OllamaClient() as llm:
                response = await llm.generate(
                    prompt=user_prompt,
                    system=system_prompt,
                    model=self.model_name,
                    temperature=self.temperature,
                    num_predict=300,  # Limit response length
                    format="json"
                )
                
                logger.debug(f"LLM response for {url}: {response[:200]}...")
                
                # Parse LLM response
                return self._parse_llm_response(response)
                
        except Exception as e:
            logger.error(f"LLM price extraction failed for {url}: {e}")
            return PriceExtractionResult(
                success=False,
                error=f"LLM extraction failed: {str(e)}"
            )
    
    def _create_system_prompt(self) -> str:
        """Create system prompt for price extraction."""
        return """You are an expert at extracting product prices from Uruguay e-commerce websites.

TASK: Extract product prices from the given page content.

FOR SINGLE PRODUCT PAGES: Extract the main product price.
FOR CATALOG/CATEGORY PAGES: Extract ALL products with their prices (up to 10 products).

OUTPUT FORMAT: JSON only, no markdown, no explanations

SINGLE PRODUCT:
{
  "found": true|false,
  "price": <numeric_value>,
  "currency": "UYU"|"USD",
  "original_text": "<exact_price_text_found>",
  "confidence": <0.0_to_1.0>
}

MULTIPLE PRODUCTS (for catalog pages):
{
  "products": [
    {
      "product_name": "<product_name>",
      "price": <numeric_value>,
      "currency": "UYU"|"USD", 
      "original_text": "<exact_price_text_found>",
      "confidence": <0.0_to_1.0>
    }
  ]
}

CRITICAL PRICE PARSING RULES:

1. CRITICAL PRICE CONVERSION - FOLLOW EXACTLY:
   - "$189" → price: 189.0 (one hundred eighty-nine)
   - "$220" → price: 220.0 (two hundred twenty)  
   - "$13.000,00" → price: 13000.0 (thirteen thousand - remove dots/commas from thousands)
   - "$1.250" → price: 1250.0 (one thousand two hundred fifty)  
   - "$45,50" → price: 45.5 (forty-five point five - comma is decimal)
   
   RULE: If you see "$189", the price is EXACTLY 189, NOT 45.5, NOT any other number.
   NEVER perform mathematical operations. Extract the literal numerical value.

2. WHAT IS NOT A PRICE - DO NOT EXTRACT THESE:
   - Wattage like "2700 W" or "1500W" - this is power specification, NOT price!
   - Model numbers like "GS1700" or "SL-PL2371FF"
   - Product codes or SKUs
   - Dimensions like "31 x 20 cm"
   - Percentages like "25% OFF"

3. CURRENCY DETECTION (CRITICAL - READ CAREFULLY):
   - $ alone = UYU (default in Uruguay)
   - U$S = USD (US dollars - VERY COMMON in Uruguay!)
   - US$ = USD
   - USD = USD
   - UY$ = UYU
   - UYU = UYU
   
   IMPORTANT: "U$S69" means 69 US dollars (USD), NOT pesos!

4. PRICE SELECTION:
   - Choose current selling price (not crossed-out/old prices)
   - If discount shown, use final discounted price
   - Ignore shipping costs, taxes shown separately

5. DECIMAL HANDLING:
   - In Uruguay: "1.250" = 1250 (dot as thousands separator)
   - In Uruguay: "45,50" = 45.5 (comma as decimal separator)
   - Always return price as a number: 1250, not "1.250"

6. VALIDATION:
   - Price should be reasonable (10-100000 UYU or 1-5000 USD typical range)
   - If price seems wrong, return {"found": false}

If no valid price found, return {"found": false}

Return ONLY the JSON object."""

    def _create_user_prompt(self, content: str, url: str, product_name: str) -> str:
        """Create user prompt with page content."""
        # Truncate content to avoid token limits
        max_content_length = 2000
        truncated_content = content[:max_content_length]
        if len(content) > max_content_length:
            truncated_content += "... [content truncated]"
        
        return f"""URL: {url}
Product Query: {product_name}

Analyze the page content below. If this is a single product page, extract ONE price. If this is a catalog/category page with multiple products, extract ALL relevant products and their prices (up to 10).

Page content:
{truncated_content}

Extract price(s) as JSON:"""

    def _parse_llm_response(self, response: str) -> PriceExtractionResult:
        """
        Parse LLM response into PriceExtractionResult.
        
        Args:
            response: Raw LLM response
            
        Returns:
            PriceExtractionResult: Parsed result
        """
        try:
            # Clean up response (remove markdown formatting if present)
            cleaned_response = self._clean_json_response(response)
            
            # Parse JSON
            data = json.loads(cleaned_response)
            
            if not data.get("found", False):
                return PriceExtractionResult(
                    success=False,
                    error="No price found in content"
                )
            
            # Extract and validate price
            price = data.get("price")
            if price is None:
                return PriceExtractionResult(
                    success=False,
                    error="Price value missing from response"
                )
            
            # Convert price to float
            price_float = float(price)
            
            # Validate price range (sanity check)
            if price_float <= 0 or price_float > 1000000:  # Reasonable price range
                return PriceExtractionResult(
                    success=False,
                    error=f"Price {price_float} outside reasonable range"
                )
            
            return PriceExtractionResult(
                success=True,
                price=price_float,
                currency=data.get("currency", "UYU"),
                original_text=data.get("original_text"),
                confidence=data.get("confidence", 0.8)
            )
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}. Response: {response}")
            return PriceExtractionResult(
                success=False,
                error=f"Invalid JSON response: {str(e)}"
            )
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid price value: {e}")
            return PriceExtractionResult(
                success=False,
                error=f"Invalid price value: {str(e)}"
            )
        except Exception as e:
            logger.error(f"Unexpected error parsing response: {e}")
            return PriceExtractionResult(
                success=False,
                error=f"Parse error: {str(e)}"
            )
    
    def _clean_json_response(self, response: str) -> str:
        """Clean LLM response to extract valid JSON by removing comments and markdown."""
        response = response.strip()
        
        # Remove markdown code blocks
        response = re.sub(r'```json\s*', '', response)
        response = re.sub(r'```\s*$', '', response)
        
        # Find JSON object
        start = response.find('{')
        end = response.rfind('}')
        
        if start != -1 and end != -1 and end > start:
            json_content = response[start:end + 1]
            
            # Remove JSON comments (// text until end of line)
            # This handles cases like: "price": 189.0, // Assuming the currency is USD...
            json_content = re.sub(r'//.*?(?=\n|$)', '', json_content, flags=re.MULTILINE)
            
            # Remove any trailing commas that might be left after comment removal
            json_content = re.sub(r',\s*}', '}', json_content)
            json_content = re.sub(r',\s*]', ']', json_content)
            
            return json_content
        
        return response
    
    def _parse_price_directly(self, price_text: str) -> Optional[float]:
        """Parse price directly from text using regex (bypass LLM errors)."""
        if not price_text:
            return None
        
        # Clean the price text
        price_text = price_text.strip()
        
        # Extract numeric part - handle Uruguay formats
        # Patterns: $189, $13.000,00, $1.250, $45,50, UYU 220, U$S35,00, USD 50, etc.
        
        # Remove currency symbols and text (including U$S, USD, UYU, $, etc.)
        # First, remove common currency patterns
        numeric_part = re.sub(r'U\$S|USD|UYU|US\$|\$|€|R\$', '', price_text, flags=re.IGNORECASE)
        # Remove any remaining letters and whitespace
        numeric_part = re.sub(r'[a-zA-Z\s]', '', numeric_part)
        
        # Handle different decimal/thousand separator patterns
        if ',' in numeric_part and '.' in numeric_part:
            # Format like 13.000,50 (thousands with dots, decimals with comma)
            numeric_part = numeric_part.replace('.', '').replace(',', '.')
        elif ',' in numeric_part and numeric_part.count(',') == 1:
            # Check if comma is decimal separator (like 45,50) or thousands (like 1,250)
            parts = numeric_part.split(',')
            if len(parts[1]) <= 2:  # Decimal separator
                numeric_part = numeric_part.replace(',', '.')
            else:  # Thousands separator
                numeric_part = numeric_part.replace(',', '')
        elif '.' in numeric_part and numeric_part.count('.') == 1:
            # Check if dot is decimal or thousands separator
            parts = numeric_part.split('.')
            if len(parts[1]) <= 2:  # Likely decimal
                pass  # Keep as is
            else:  # Likely thousands separator
                numeric_part = numeric_part.replace('.', '')
        
        try:
            parsed_price = float(numeric_part)
            logger.debug(f"Direct price parsing: '{price_text}' → {parsed_price}")
            return parsed_price
        except (ValueError, TypeError):
            logger.warning(f"Could not parse price directly from: '{price_text}'")
            return None
    
    def _try_direct_text_extraction(self, page_content: str) -> Optional[Dict]:
        """
        Try to extract price directly from page content using regex patterns.
        This is faster and more reliable than LLM for well-formatted prices.
        
        Handles Uruguay-specific formats:
        - U$S 69 or U$S69 → USD 69
        - US$ 99 → USD 99
        - USD 31,50 → USD 31.50
        - $ 1.090,00 → UYU 1090
        - $670 → UYU 670
        
        Returns:
            Dict with product_name and price_extraction, or None if no confident match
        """
        if not page_content:
            return None
        
        # Look for price patterns in content (first 3000 chars for performance)
        content = page_content[:3000]
        
        # Pattern 1: U$S or US$ followed by number (USD)
        usd_patterns = [
            r'U\$S\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)',  # U$S 69 or U$S69,00
            r'US\$\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)',  # US$ 99
            r'USD\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)',   # USD 99
        ]
        
        for pattern in usd_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                # Take the first match (usually the main price)
                price_str = matches[0]
                price = self._parse_price_directly(price_str)
                if price and price >= 10:  # Sanity check: appliances cost at least $10 USD
                    logger.info(f"Direct extraction found USD price: {price} from '{price_str}'")
                    return {
                        'product_name': 'unknown product',
                        'price_extraction': PriceExtractionResult(
                            success=True,
                            price=price,
                            currency="USD",
                            original_text=f"U$S {price_str}",
                            confidence=0.95
                        )
                    }
        
        # Pattern 2: $ followed by number (UYU - only if no U$S found)
        # Be more careful here as $ alone is common
        uyu_patterns = [
            r'(?<![US])\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)',  # $ 1.090,00 (thousands with dots)
            r'UYU\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)',     # UYU 1520
        ]
        
        for pattern in uyu_patterns:
            matches = re.findall(pattern, content)
            if matches:
                # Take the first match
                price_str = matches[0]
                price = self._parse_price_directly(price_str)
                if price and price >= 100:  # Sanity check: appliances cost at least $100 UYU
                    logger.info(f"Direct extraction found UYU price: {price} from '{price_str}'")
                    return {
                        'product_name': 'unknown product',
                        'price_extraction': PriceExtractionResult(
                            success=True,
                            price=price,
                            currency="UYU",
                            original_text=f"$ {price_str}",
                            confidence=0.90
                        )
                    }
        
        # No confident match found
        return None

    def _normalize_product_name(self, name: Optional[str]) -> str:
        if not name:
            return ""
        text = name.lower()
        text = re.sub(r"[^a-z0-9\s]+", " ", text)
        tokens = [t for t in text.split() if len(t) > 2]
        stopwords = {
            "nuevo", "oferta", "original", "promo", "promocion", "promoción",
            "envio", "envío", "gratis", "pack", "combo",
        }
        tokens = [t for t in tokens if t not in stopwords]
        return " ".join(tokens)