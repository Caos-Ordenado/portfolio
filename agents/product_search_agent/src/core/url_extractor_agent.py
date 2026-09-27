from typing import List, Optional, Dict, Any
import re
from urllib.parse import urlparse
from shared.logging import setup_logger
from shared.llm_client import LLMClient, MODEL_EXTRACT
from src.api.models import BraveSearchResult, ExtractedUrlInfo, BraveApiHit
from src.core.utils import sanitize_ecommerce_url

logger = setup_logger("url_extractor_agent")

class UrlExtractorAgent:
    def __init__(self, llm_threshold: int = 20, model_name: str = MODEL_EXTRACT, temperature: float = 0.1):
        """
        Initialize UrlExtractorAgent with pre-filtering capabilities.
        
        Args:
            llm_threshold: Minimum number of URLs to trigger LLM-based filtering
            model_name: LLM model for bulk classification
            temperature: LLM temperature for classification
        """
        self.llm_threshold = llm_threshold
        self.model_name = model_name
        self.temperature = temperature
        logger.info(f"UrlExtractorAgent initialized - CONSTRUCTOR CALLED V4 with LLM threshold: {llm_threshold}")

    async def __aenter__(self):
        logger.debug("Entering UrlExtractorAgent context")
        # No external resources to manage for now
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("Exiting UrlExtractorAgent context")
        # No external resources to clean up

    def _apply_pattern_filtering(self, urls: List[ExtractedUrlInfo]) -> List[ExtractedUrlInfo]:
        """
        Stage 1: Apply pattern-based filtering to remove obviously non-product URLs.
        """
        # Define domains that are irrelevant for product searches
        # These are sites that might match Uruguay but don't sell products
        EXCLUDE_DOMAINS = {
            # Travel and tourism (not e-commerce)
            'tiendaviajes.com.uy',
            'viajes.com.uy',
            'despegar.com.uy',
            'booking.com',
            'tripadvisor.com',
            
            # Real estate (not products)
            'gallito.com.uy',
            'infocasas.com.uy',
            'inmuebles.com.uy',
            
            # News and media (not e-commerce)
            'elpais.com.uy',
            'montevideo.com.uy',
            'subrayado.com.uy',
            'elobservador.com.uy',
            'ladiaria.com.uy',
            
            # Job boards (not products)
            'buscojobs.com.uy',
            'computrabajo.com.uy',
            'empleos.com.uy',
            
            # Social media and directories
            'facebook.com',
            'instagram.com',
            'twitter.com',
            'linkedin.com',
            'pinterest.com',
            'youtube.com',
            
            # Information sites (not e-commerce)
            'wikipedia.org',
            'gub.uy',  # Government sites
        }
        
        # Define patterns for different URL types
        EXCLUDE_PATTERNS = [
            # Navigation and utility pages
            r'/ayuda/', r'/help/', r'/support/', r'/contacto/', r'/contact/',
            r'/blog/', r'/news/', r'/noticias/', r'/faq/', r'/terms/', r'/privacy/',
            r'/politicas/', r'/about/', r'/acerca/', r'/nosotros/', r'/quienes-somos/',
            r'/legal/', r'/cookies/', r'/glossary/', r'/glosario/', r'/sitemap/',
            
            # Authentication and user pages
            r'/login/', r'/signin/', r'/register/', r'/signup/', r'/mi-cuenta/',
            r'/my-account/', r'/profile/', r'/perfil/', r'/dashboard/', r'/admin/',
            r'/checkout/', r'/cart/', r'/carrito/', r'/wish/', r'/favoritos/',
            
            # API and technical endpoints
            r'/api/', r'/ajax/', r'/json/', r'/xml/', r'/rss/', r'/feed/',
            r'/autocomplete/', r'/suggest/', r'/search-suggest/',
            
            # Generic listing pages (keep specific listings)
            r'^[^/]+/(?:categories?|categorias?|sections?|secciones?)/?$',
            r'/browse/', r'/explorar/', r'/directory/', r'/directorio/',
            
            # File extensions that are not product pages
            r'\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|exe|dmg)$',
            r'\.(jpg|jpeg|png|gif|svg|webp|mp4|mp3|wav)$',
        ]
        
        # Define patterns for high-priority URLs (product pages)
        INCLUDE_PATTERNS = [
            # Product page patterns for major e-commerce sites
            r'/p/[A-Z]{3}\d+',  # MercadoLibre: /p/MLU12345
            r'/product/',       # Generic product pages
            r'/producto/',      # Spanish product pages
            r'/item/',          # Item pages
            r'/articulo/',      # Spanish article pages
            r'/dp/[A-Z0-9]+',   # Amazon-style product pages
            r'/gp/product/',    # Amazon generic product
            r'/products?/[^/]+$', # Product with ID at end
        ]
        
        filtered_urls = []
        excluded_count = 0
        domain_excluded_count = 0
        
        for url_info in urls:
            url = url_info.url.lower()
            
            # First check: domain blocklist (most efficient check)
            try:
                parsed = urlparse(url)
                domain = parsed.netloc.lower()
                # Check if domain matches any blocked domain (including subdomains)
                is_blocked_domain = any(
                    domain == blocked or domain.endswith('.' + blocked) 
                    for blocked in EXCLUDE_DOMAINS
                )
                if is_blocked_domain:
                    domain_excluded_count += 1
                    logger.debug(f"Excluded URL by domain blocklist: {url_info.url}")
                    continue
            except Exception:
                pass  # If URL parsing fails, continue with pattern check
            
            # Check exclude patterns
            excluded = False
            for pattern in EXCLUDE_PATTERNS:
                if re.search(pattern, url, re.IGNORECASE):
                    excluded = True
                    excluded_count += 1
                    logger.debug(f"Excluded URL by pattern '{pattern}': {url_info.url}")
                    break
            
            if not excluded:
                # Check if it matches high-priority patterns (automatic include)
                is_high_priority = any(re.search(pattern, url, re.IGNORECASE) for pattern in INCLUDE_PATTERNS)
                if is_high_priority:
                    logger.debug(f"High-priority URL included: {url_info.url}")
                
                filtered_urls.append(url_info)
        
        total_excluded = excluded_count + domain_excluded_count
        logger.info(f"Pattern filtering: {len(urls)} → {len(filtered_urls)} URLs ({total_excluded} excluded, {domain_excluded_count} by domain blocklist)")
        return filtered_urls

    def _apply_advanced_duplicate_detection(self, urls: List[ExtractedUrlInfo]) -> List[ExtractedUrlInfo]:
        """
        Stage 2: Advanced duplicate detection beyond simple URL matching.
        """
        seen_normalized = set()
        seen_domains = {}
        unique_urls = []
        
        for url_info in urls:
            # Normalize URL for comparison
            normalized = self._normalize_url(url_info.url)
            
            # Check for exact normalized duplicates
            if normalized in seen_normalized:
                logger.debug(f"Duplicate URL (normalized): {url_info.url}")
                continue
            
            # Check for domain over-representation
            domain = urlparse(url_info.url).netloc
            domain_count = seen_domains.get(domain, 0)
            
            # Limit URLs per domain to prevent spam
            MAX_URLS_PER_DOMAIN = 10
            if domain_count >= MAX_URLS_PER_DOMAIN:
                logger.debug(f"Domain limit reached for {domain}: {url_info.url}")
                continue
            
            seen_normalized.add(normalized)
            seen_domains[domain] = domain_count + 1
            unique_urls.append(url_info)
        
        logger.info(f"Duplicate detection: {len(urls)} → {len(unique_urls)} URLs")
        return unique_urls

    def _normalize_url(self, url: str) -> str:
        """
        Normalize URL for duplicate detection.
        """
        # Remove common tracking parameters
        tracking_params = [
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
            'ref', 'referer', 'source', 'campaign', 'fbclid', 'gclid', 'dclid',
            '_ga', '_gac', 'mc_cid', 'mc_eid', 'affiliate', 'partner'
        ]
        
        parsed = urlparse(url.lower().strip())
        
        # Remove www prefix
        netloc = parsed.netloc
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        
        # Remove trailing slashes and normalize path
        path = parsed.path.rstrip('/')
        
        # Build normalized URL (without query params for now - can be enhanced)
        normalized = f"{parsed.scheme}://{netloc}{path}"
        
        return normalized

    async def _apply_llm_bulk_filtering(self, urls: List[ExtractedUrlInfo]) -> List[ExtractedUrlInfo]:
        """
        Stage 3: LLM-based bulk filtering for large URL sets.
        Only triggered when URL count exceeds threshold.
        """
        if len(urls) <= self.llm_threshold:
            logger.info(f"URL count ({len(urls)}) below LLM threshold ({self.llm_threshold}), skipping LLM filtering")
            return urls
        
        logger.info(f"Applying LLM bulk filtering to {len(urls)} URLs (threshold: {self.llm_threshold})")
        
        try:
            async with LLMClient(model=self.model_name) as llm:
                # Prepare URLs for bulk classification
                url_list = [{"url": url_info.url, "title": url_info.title or ""} for url_info in urls]
                
                prompt = self._build_bulk_classification_prompt(url_list)
                
                response = await llm.generate(
                    prompt=prompt,
                    format="json",
                    temperature=self.temperature
                )
                
                # Parse LLM response and filter URLs
                filtered_urls = self._parse_llm_bulk_response(response, urls)
                
                logger.info(f"LLM bulk filtering: {len(urls)} → {len(filtered_urls)} URLs")
                return filtered_urls
                
        except Exception as e:
            logger.warning(f"LLM bulk filtering failed: {e}. Returning original URLs")
            return urls

    def _build_bulk_classification_prompt(self, url_list: List[Dict[str, str]]) -> str:
        """
        Build prompt for bulk URL classification.
        """
        urls_text = "\n".join([f"{i+1}. {item['url']} (Title: {item['title'][:100]})" 
                              for i, item in enumerate(url_list)])
        
        return f"""Analyze this list of URLs and identify which ones are likely to be PRODUCT pages (not category/listing pages).

URLs to analyze:
{urls_text}

Return a JSON object with an array of URL indices (1-based) that are likely PRODUCT pages:

{{
  "product_url_indices": [1, 3, 7, 12],
  "reasoning": "Brief explanation of filtering criteria"
}}

Focus on:
- URLs with specific product identifiers (IDs, SKUs)
- URLs ending with product-specific paths
- Avoid category listings, search results, or navigation pages
- Prioritize e-commerce product detail pages"""

    def _parse_llm_bulk_response(self, response: str, original_urls: List[ExtractedUrlInfo]) -> List[ExtractedUrlInfo]:
        """
        Parse LLM response and return filtered URLs.
        """
        try:
            import json
            result = json.loads(response)
            indices = result.get("product_url_indices", [])
            
            # Convert 1-based indices to 0-based and filter URLs
            filtered_urls = []
            for idx in indices:
                if 1 <= idx <= len(original_urls):
                    filtered_urls.append(original_urls[idx - 1])
            
            reasoning = result.get("reasoning", "No reasoning provided")
            logger.info(f"LLM bulk filtering reasoning: {reasoning}")
            
            return filtered_urls
            
        except Exception as e:
            logger.error(f"Failed to parse LLM bulk response: {e}. Response: {response[:200]}")
            return original_urls

    async def extract_product_url_info(self, all_brave_results: Optional[List[BraveSearchResult]]) -> List[ExtractedUrlInfo]:
        """
        Enhanced URL extraction with 3-stage pre-filtering pipeline:
        1. Pattern-based filtering (rules-based)
        2. Advanced duplicate detection
        3. LLM-based bulk classification (when needed)
        """
        if not all_brave_results:
            return []

        # Step 1: Extract raw URLs from Brave results
        extracted_candidates: List[ExtractedUrlInfo] = []
        seen_urls = set()  # Basic duplicate tracking during extraction
        
        for brave_result_item in all_brave_results:
            if not brave_result_item.results:
                continue

            web_search_results = brave_result_item.results.get('web', {}).get('results', [])
            
            if not isinstance(web_search_results, list):
                logger.warning(f"Expected a list for web_search_results for query '{brave_result_item.query}', but got {type(web_search_results)}. Skipping.")
                continue

            for hit_data in web_search_results:
                if not isinstance(hit_data, dict):
                    logger.warning(f"Skipping non-dictionary item in Brave web search results: {hit_data} for query '{brave_result_item.query}'")
                    continue
                
                try:
                    brave_hit = BraveApiHit(**hit_data)
                    if brave_hit.url:
                        # Sanitize URL to fix malformed patterns from cached results
                        sanitized_url = sanitize_ecommerce_url(brave_hit.url)
                        
                        if sanitized_url and sanitized_url not in seen_urls:
                            seen_urls.add(sanitized_url)
                            extracted_candidates.append(
                                ExtractedUrlInfo(
                                    url=sanitized_url,
                                    title=brave_hit.title,
                                    snippet=brave_hit.description,
                                    source_query=brave_result_item.query
                                )
                            )
                except Exception as e:
                    logger.warning(f"Could not parse Brave hit data: {hit_data} for query '{brave_result_item.query}'. Error: {e}")
        
        initial_count = len(extracted_candidates)
        logger.info(f"Initial extraction: {initial_count} unique URLs from Brave results")
        
        if not extracted_candidates:
            return []
        
        # Step 2: Apply 3-stage filtering pipeline
        try:
            # Stage 1: Pattern-based filtering
            filtered_candidates = self._apply_pattern_filtering(extracted_candidates)
            
            # Stage 2: Advanced duplicate detection
            filtered_candidates = self._apply_advanced_duplicate_detection(filtered_candidates)
            
            # Stage 3: LLM-based bulk filtering (if needed)
            filtered_candidates = await self._apply_llm_bulk_filtering(filtered_candidates)
            
            final_count = len(filtered_candidates)
            reduction_percentage = ((initial_count - final_count) / initial_count * 100) if initial_count > 0 else 0
            
            logger.info(f"Pre-filtering pipeline completed: {initial_count} → {final_count} URLs ({reduction_percentage:.1f}% reduction)")
            
            return filtered_candidates
            
        except Exception as e:
            logger.error(f"Error in pre-filtering pipeline: {e}. Returning original candidates")
            return extracted_candidates 