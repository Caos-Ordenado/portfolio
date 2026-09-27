from typing import List, Optional, Set
from urllib.parse import urlparse
import json
import os

from shared.logging import setup_logger
from shared.web_crawler_client import WebCrawlerClient
from shared.llm_client import LLMClient, MODEL_EXTRACT
from shared.renderer_client import RendererClient
from shared.utils import same_domain, dedupe_urls_preserve_order
from .batch_content_retriever import BatchContentRetriever
from .utils import (
    is_likely_product_url,
    url_matches_query,
    remove_duplicated_path_segments,
    extract_links_from_html,
    is_mercadolibre_listing_url,
    is_mercadolibre_product_url,
)

logger = setup_logger("category_expansion")


class CategoryExpansionAgent:
    def __init__(self, per_domain_cap: int = 8, global_cap: int = 50):
        self.per_domain_cap = per_domain_cap
        self.global_cap = global_cap
        self.batch_retriever = BatchContentRetriever()
        logger.info(f"CategoryExpansionAgent initialized with batch retrieval enabled")
    
    async def _llm_classify_urls(self, urls: List[str], query: str) -> List[str]:
        """
        Use LLM to classify URLs as product-related based on the search query.
        This is a fallback when structural patterns aren't sufficient.
        """
        if not urls or not query:
            return []
        
        try:
            # Batch URLs for efficiency (limit to reasonable batch size)
            batch_size = 20
            classified_product_urls = []
            
            for i in range(0, len(urls), batch_size):
                batch_urls = urls[i:i + batch_size]
                
                prompt = f"""Given the search query "{query}", classify which of these URLs are likely product pages (not categories, search results, or navigation pages).

URLs to classify:
{chr(10).join(f"{idx+1}. {url}" for idx, url in enumerate(batch_urls))}

Return ONLY a JSON array of the URL numbers (1-{len(batch_urls)}) that are likely product pages.
Example: [1, 3, 5] means URLs 1, 3, and 5 are product pages.
Return empty array [] if none are product pages."""

                try:
                    async with LLMClient() as llm:
                        response = await llm.generate(
                            prompt=prompt,
                            model=MODEL_EXTRACT,
                            temperature=0.0,
                            format="json"
                        )
                        
                        # Parse the response
                        indices = json.loads(response.strip())
                        if isinstance(indices, list):
                            for idx in indices:
                                if isinstance(idx, int) and 1 <= idx <= len(batch_urls):
                                    classified_product_urls.append(batch_urls[idx - 1])
                        
                        logger.debug(f"LLM classified {len(indices)}/{len(batch_urls)} URLs as products")
                        
                except (json.JSONDecodeError, Exception) as e:
                    logger.debug(f"LLM classification failed for batch: {e}")
                    continue
            
            logger.info(f"LLM classification: {len(classified_product_urls)}/{len(urls)} URLs identified as products")
            return classified_product_urls
            
        except Exception as e:
            logger.warning(f"LLM URL classification failed: {e}")
            return []

    async def _expand_with_renderer(
        self, 
        missing_urls: List[str], 
        product_links: List[str], 
        query_terms: Optional[List[str]] = None,
        timeout_ms: int = 30000
    ) -> None:
        """
        Use Playwright renderer as fallback for JS-heavy sites that the crawler couldn't process.
        Modifies product_links in place.
        """
        if not missing_urls:
            return
        
        renderer_base_url = os.getenv("RENDERER_URL", "http://home.server:30080/renderer")
        
        try:
            async with RendererClient(base_url=renderer_base_url) as renderer:
                for url in missing_urls:
                    if len(product_links) >= self.global_cap:
                        logger.debug(f"Reached global cap ({self.global_cap}), stopping renderer fallback")
                        break
                    
                    try:
                        logger.debug(f"Renderer fallback: fetching {url}")
                        
                        # Use render_html to get JS-rendered content
                        result = await renderer.render_html(
                            url=url,
                            wait_for_selector="body",
                            timeout_ms=timeout_ms,
                            viewport_randomize=True,
                        )
                        
                        html_content = result.get("html", "")
                        if not html_content:
                            logger.debug(f"Renderer returned no HTML for {url}")
                            continue
                        
                        # Extract links from rendered HTML
                        domain = urlparse(url).netloc
                        count_domain = sum(1 for l in product_links if urlparse(l).netloc == domain)
                        
                        html_links = extract_links_from_html(html_content, url)
                        # MercadoLibre special-case: listing pages often link to product pages on other ML subdomains
                        if "mercadolibre.com.uy" in domain:
                            allowed_hosts = {
                                "listado.mercadolibre.com.uy",
                                "articulo.mercadolibre.com.uy",
                                "www.mercadolibre.com.uy",
                                "mercadolibre.com.uy",
                            }
                            same_domain_links = [
                                l for l in html_links
                                if urlparse(l).netloc.lower() in allowed_hosts
                            ]
                        else:
                            same_domain_links = [l for l in html_links if same_domain(url, l)]
                        
                        # Deduplicate and sanitize
                        unique_links = dedupe_urls_preserve_order(same_domain_links)
                        sanitized_links = [remove_duplicated_path_segments(l) for l in unique_links]
                        sanitized_links = dedupe_urls_preserve_order(sanitized_links)
                        
                        # Filter for product URLs and query relevance
                        # MercadoLibre: prefer deterministic product URL patterns
                        if is_mercadolibre_listing_url(url):
                            filtered = [l for l in sanitized_links if is_mercadolibre_product_url(l)]
                        else:
                            filtered = [l for l in sanitized_links if is_likely_product_url(l, query_terms)]
                        filtered = [l for l in filtered if url_matches_query(l, query_terms)]
                        
                        logger.info(f"Renderer fallback {domain}: {len(same_domain_links)} links → {len(filtered)} product URLs")
                        
                        # Add to product_links respecting caps
                        for l in filtered:
                            if count_domain >= self.per_domain_cap:
                                logger.debug(f"Reached per-domain cap ({self.per_domain_cap}) for {domain}")
                                break
                            if len(product_links) >= self.global_cap:
                                break
                            if l not in product_links:  # Avoid duplicates
                                product_links.append(l)
                                count_domain += 1
                        
                    except Exception as e:
                        logger.warning(f"Renderer fallback failed for {url}: {e}")
                        continue
                        
        except Exception as e:
            logger.error(f"Renderer fallback initialization failed: {e}")

    async def expand(self, category_urls: List[str], query_terms: Optional[List[str]] = None, timeout_ms: int = 30000) -> List[str]:
        if not category_urls:
            return []
        
        logger.info(f"🚀 OPTIMIZATION: Batch expanding {len(category_urls)} category URLs")
        product_links: List[str] = []
        
        try:
            # Use bulk crawl with link extraction for better performance
            async with WebCrawlerClient() as crawler:
                response = await crawler.crawl(
                    urls=category_urls,
                    max_pages=1,  # Only crawl the category pages themselves
                    max_depth=1,  # No link following
                    timeout=timeout_ms,
                    max_total_time=120,  # 2 minute total timeout
                    max_concurrent_pages=3  # Reasonable concurrency for category pages
                )
                
                if response.success and response.results:
                    for result in response.results:
                        domain = urlparse(result.url).netloc
                        count_domain = sum(1 for l in product_links if urlparse(l).netloc == domain)
                        
                        # Collect all links from multiple sources
                        all_links = []
                        
                        # 1. Use web crawler's extracted links
                        if result.links:
                            if "mercadolibre.com.uy" in domain:
                                allowed_hosts = {
                                    "listado.mercadolibre.com.uy",
                                    "articulo.mercadolibre.com.uy",
                                    "www.mercadolibre.com.uy",
                                    "mercadolibre.com.uy",
                                }
                                crawler_links = [l for l in result.links if urlparse(l).netloc.lower() in allowed_hosts]
                            else:
                                crawler_links = [l for l in result.links if same_domain(result.url, l)]
                            all_links.extend(crawler_links)
                            logger.debug(f"Web crawler found {len(crawler_links)} same-domain links for {domain}")
                        
                        # 2. Extract additional links from HTML content
                        if hasattr(result, 'text') and result.text:
                            html_links = extract_links_from_html(result.text, result.url)
                            if "mercadolibre.com.uy" in domain:
                                allowed_hosts = {
                                    "listado.mercadolibre.com.uy",
                                    "articulo.mercadolibre.com.uy",
                                    "www.mercadolibre.com.uy",
                                    "mercadolibre.com.uy",
                                }
                                htmlsame_domain = [l for l in html_links if urlparse(l).netloc.lower() in allowed_hosts]
                            else:
                                htmlsame_domain = [l for l in html_links if same_domain(result.url, l)]
                            all_links.extend(htmlsame_domain)
                            logger.debug(f"HTML parsing found {len(htmlsame_domain)} additional same-domain links for {domain}")
                        
                        # 3. Deduplicate, sanitize, and filter for product URLs
                        unique_links = dedupe_urls_preserve_order(all_links)
                        # Sanitize URLs to remove duplicated path segments
                        sanitized_links = [remove_duplicated_path_segments(l) for l in unique_links]
                        # Re-dedupe after sanitization (some may now be duplicates)
                        sanitized_links = dedupe_urls_preserve_order(sanitized_links)
                        # Filter for product URLs
                        if is_mercadolibre_listing_url(result.url):
                            filtered = [l for l in sanitized_links if is_mercadolibre_product_url(l)]
                        else:
                            filtered = [l for l in sanitized_links if is_likely_product_url(l, query_terms)]
                        # Filter for query relevance (remove unrelated categories)
                        filtered = [l for l in filtered if url_matches_query(l, query_terms)]
                        
                        logger.info(f"Domain {domain}: {len(all_links)} total links → {len(unique_links)} unique → {len(filtered)} product URLs")
                        
                        # Apply per-domain cap
                        for l in filtered:
                            if count_domain >= self.per_domain_cap:
                                logger.debug(f"Reached per-domain cap ({self.per_domain_cap}) for {domain}")
                                break
                            product_links.append(l)
                            count_domain += 1
                            
                            if len(product_links) >= self.global_cap:
                                logger.debug(f"Reached global cap ({self.global_cap})")
                                break
                        
                        if len(product_links) >= self.global_cap:
                            break
                            
                    logger.info(f"Category expansion found {len(product_links)} product URLs from {len(category_urls)} categories")
                    
                    # Check for missing URLs - crawler may have failed on JS-heavy sites
                    requested_urls: Set[str] = set(category_urls)
                    returned_urls: Set[str] = {r.url for r in response.results if r}
                    missing_urls = requested_urls - returned_urls
                    
                    if missing_urls and len(product_links) < self.global_cap:
                        logger.info(f"Crawler missed {len(missing_urls)} URLs, trying renderer fallback for JS-heavy sites")
                        await self._expand_with_renderer(
                            missing_urls=list(missing_urls),
                            product_links=product_links,
                            query_terms=query_terms,
                            timeout_ms=timeout_ms
                        )
                else:
                    logger.warning(f"Bulk category expansion failed: {response.error if hasattr(response, 'error') else 'Unknown error'}")
                    
        except Exception as e:
            logger.error(f"Category expansion batch request failed: {e}")
            
        # If we got very few results, try individual crawls as fallback
        if len(product_links) < 5 and len(category_urls) <= 3:
            logger.info("Low results from bulk crawl, trying individual page crawls as fallback")
            try:
                async with WebCrawlerClient() as crawler:
                    for category_url in category_urls:
                        if len(product_links) >= self.global_cap:
                            break
                            
                        try:
                            single_response = await crawler.crawl_single(
                                url=category_url,
                                timeout=timeout_ms
                            )
                            
                            if single_response.success and single_response.result:
                                result = single_response.result
                                domain = urlparse(result.url).netloc
                                count_domain = sum(1 for l in product_links if urlparse(l).netloc == domain)
                                
                                # Extract links using same logic as bulk crawl
                                all_links = []
                                
                                if result.links:
                                    crawler_links = [l for l in result.links if same_domain(result.url, l)]
                                    all_links.extend(crawler_links)
                                
                                if hasattr(result, 'text') and result.text:
                                    html_links = extract_links_from_html(result.text, result.url)
                                    htmlsame_domain = [l for l in html_links if same_domain(result.url, l)]
                                    all_links.extend(htmlsame_domain)
                                
                                unique_links = dedupe_urls_preserve_order(all_links)
                                # Sanitize URLs to remove duplicated path segments
                                sanitized_links = [remove_duplicated_path_segments(l) for l in unique_links]
                                sanitized_links = dedupe_urls_preserve_order(sanitized_links)
                                # Filter for product URLs
                                filtered = [l for l in sanitized_links if is_likely_product_url(l, query_terms)]
                                # Filter for query relevance (remove unrelated categories)
                                filtered = [l for l in filtered if url_matches_query(l, query_terms)]
                                
                                logger.info(f"Individual crawl {domain}: {len(filtered)} product URLs found")
                                
                                for l in filtered:
                                    if count_domain >= self.per_domain_cap or len(product_links) >= self.global_cap:
                                        break
                                    product_links.append(l)
                                    count_domain += 1
                            
                        except Exception as e:
                            logger.warning(f"Individual crawl failed for {category_url}: {e}")
                            
            except Exception as e:
                logger.error(f"Fallback individual crawls failed: {e}")
        
        # Final LLM fallback if we still have very few results and query terms available
        if len(product_links) < 3 and query_terms:
            logger.info("Very low results, trying LLM classification as final fallback")
            try:
                # Collect all unique URLs from previous crawls for LLM analysis
                all_collected_urls = []
                
                async with WebCrawlerClient() as crawler:
                    for category_url in category_urls:
                        try:
                            single_response = await crawler.crawl_single(
                                url=category_url,
                                timeout=15000  # Shorter timeout for fallback
                            )
                            
                            if single_response.success and single_response.result:
                                result = single_response.result
                                if result.links:
                                    same_domain_links = [l for l in result.links if same_domain(result.url, l)]
                                    all_collected_urls.extend(same_domain_links)
                                    
                        except Exception as e:
                            logger.debug(f"LLM fallback crawl failed for {category_url}: {e}")
                            continue
                
                if all_collected_urls:
                    # Remove duplicates and filter out obvious non-products
                    unique_urls = dedupe_urls_preserve_order(all_collected_urls)
                    # Basic filtering to reduce LLM load
                    candidate_urls = [
                        u for u in unique_urls 
                        if not any(bad in u.lower() for bad in ("/search", "/category", "?page=", "#"))
                    ]
                    
                    if candidate_urls:
                        query_string = " ".join(query_terms) if query_terms else ""
                        llm_classified = await self._llm_classify_urls(candidate_urls[:50], query_string)  # Limit for performance
                        
                        # Add LLM results to our product links (with query relevance filter)
                        added_count = 0
                        for url in llm_classified:
                            if len(product_links) >= self.global_cap:
                                break
                            # Apply query relevance filter
                            if not url_matches_query(url, query_terms):
                                continue
                            if url not in product_links:  # Avoid duplicates
                                product_links.append(url)
                                added_count += 1
                        
                        logger.info(f"LLM fallback added {added_count} additional product URLs (filtered from {len(llm_classified)})")
                        
            except Exception as e:
                logger.warning(f"LLM fallback classification failed: {e}")
            
        logger.info(f"Final category expansion result: {len(product_links)} product URLs")
        return dedupe_urls_preserve_order(product_links)[: self.global_cap]


