import asyncio
import json
from typing import List, Dict, Any, Optional
import re
import httpx # For potential errors from OllamaClient
import os
import hashlib
from urllib.parse import urlparse

from shared.logging import setup_logger
from shared.ollama_client import OllamaClient
from shared.redis_client import RedisClient
from shared.utils import strip_json_code_block, remove_json_comments, extract_fields_from_partial_json
from src.api.models import ExtractedUrlInfo, IdentifiedPageCandidate
from src.core.utils import is_mercadolibre_listing_url, is_mercadolibre_product_url
from src.core.utils.ecommerce_url_utils import URUGUAY_TLDS

logger = setup_logger("product_page_candidate_identifier")


class ProductPageCandidateIdentifierAgent:
    def __init__(self, model_name="qwen3:latest", temperature=0.1):
        self.model_name = model_name
        self.temperature = temperature
        self.page_type_cache_enabled = os.getenv("PAGE_TYPE_CACHE_ENABLED", "true").lower() == "true"
        self.page_type_cache_ttl_seconds = int(os.getenv("PAGE_TYPE_CACHE_TTL_SECONDS", "21600"))  # 6 hours
        logger.info(f"ProductPageCandidateIdentifierAgent initialized with model: {model_name}, temp: {temperature}")

    async def __aenter__(self):
        logger.debug("ProductPageCandidateIdentifierAgent context entered")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("ProductPageCandidateIdentifierAgent context exited")

    def _is_uruguay_url(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urlparse(url.lower())
            domain = parsed.netloc
            path_and_query = f"{parsed.path}?{parsed.query}"
            if any(domain.endswith(tld) for tld in URUGUAY_TLDS):
                return True
            if "uruguay" in domain or "uruguay" in path_and_query:
                return True
            if "/uy/" in path_and_query or "montevideo" in path_and_query:
                return True
        except Exception:
            return False
        return False

    def _page_type_cache_key(self, url: str, product_name: str) -> str:
        raw = f"{url}|{product_name}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        return f"page_type:{digest}"

    async def _get_cached_page_type(self, url: str, product_name: str) -> Optional[Dict[str, Any]]:
        try:
            async with RedisClient() as redis_client:
                if not await redis_client.health_check():
                    return None
                value = await redis_client.get(self._page_type_cache_key(url, product_name))
                if not value:
                    return None
                return json.loads(value)
        except Exception:
            return None

    async def _set_cached_page_type(self, url: str, product_name: str, payload: Dict[str, Any]) -> None:
        try:
            async with RedisClient() as redis_client:
                if not await redis_client.health_check():
                    return
                await redis_client.set(
                    self._page_type_cache_key(url, product_name),
                    payload,
                    ex=self.page_type_cache_ttl_seconds,
                )
        except Exception:
            return

    async def _classify_url_with_llm(self, url_info: ExtractedUrlInfo, product_name: str) -> IdentifiedPageCandidate:
        # Deterministic MercadoLibre overrides (no prompt changes):
        # - listado.* is a listing/search page -> CATEGORY
        # - articulo.* and /p/MLU... are PRODUCT
        try:
            if not self._is_uruguay_url(url_info.url):
                candidate = IdentifiedPageCandidate(
                    url=url_info.url,
                    original_title=url_info.title,
                    original_snippet=url_info.snippet,
                    source_query=url_info.source_query,
                    page_type="EXCLUDE_NON_URUGUAY",
                    reasoning="Deterministic exclusion: URL is not Uruguay-relevant.",
                )
                if self.page_type_cache_enabled:
                    await self._set_cached_page_type(url_info.url, product_name, candidate.model_dump())
                return candidate
            if is_mercadolibre_listing_url(url_info.url):
                candidate = IdentifiedPageCandidate(
                    url=url_info.url,
                    original_title=url_info.title,
                    original_snippet=url_info.snippet,
                    source_query=url_info.source_query,
                    page_type="CATEGORY",
                    category_name="MercadoLibre listing",
                    reasoning="Deterministic override: MercadoLibre listing/search page.",
                )
                if self.page_type_cache_enabled:
                    await self._set_cached_page_type(url_info.url, product_name, candidate.model_dump())
                return candidate
            if is_mercadolibre_product_url(url_info.url):
                candidate = IdentifiedPageCandidate(
                    url=url_info.url,
                    original_title=url_info.title,
                    original_snippet=url_info.snippet,
                    source_query=url_info.source_query,
                    page_type="PRODUCT",
                    identified_product_name=(url_info.title or product_name),
                    reasoning="Deterministic override: MercadoLibre product URL pattern.",
                )
                if self.page_type_cache_enabled:
                    await self._set_cached_page_type(url_info.url, product_name, candidate.model_dump())
                return candidate
        except Exception:
            # If URL parsing fails for any reason, fall back to LLM path.
            pass

        if self.page_type_cache_enabled:
            cached = await self._get_cached_page_type(url_info.url, product_name)
            if cached:
                return IdentifiedPageCandidate(**cached)

        system_prompt = f"""
You are an AI assistant that analyzes web page content (title, URL, and a snippet of text) to determine if it's a product page, a category page, a blog post, or 'other'.
You are also given the original product name the user is searching for: "{product_name}"

Respond with a JSON object containing ONLY the following fields:
- "page_type": (string) One of "PRODUCT", "CATEGORY", "BLOG", "OTHER", or "EXCLUDE_NON_URUGUAY" (if the URL is not for Uruguay).
- "identified_product_name": (string, OPTIONAL) If page_type is "PRODUCT", the product name identified on the page. Otherwise omit.
- "category_name": (string, OPTIONAL) If page_type is "CATEGORY", the name of the category. Otherwise omit.
- "reasoning": (string, OPTIONAL) A brief explanation for your classification.

Example for a PRODUCT page:
{{
  "page_type": "PRODUCT",
  "identified_product_name": "Specific Product Model X",
  "reasoning": "Page title and snippet suggest a specific product."
}}

Example for a CATEGORY page:
{{
  "page_type": "CATEGORY",
  "category_name": "Winter Jackets",
  "reasoning": "Lists multiple winter jackets."
}}

IMPORTANT: Only classify a page as "PRODUCT" if it is an individual product page FOR SALE, not a category, collection, listing, recipe, or content page.

EXCLUDE these from PRODUCT classification:
- Recipe pages (gastronomia.montevideo.com.uy, cooking sites) - classify as "OTHER"
- News/blog articles about products - classify as "OTHER" 
- Directory listings (foodbevg.com) - classify as "OTHER"
- Category/collection pages with multiple products - classify as "CATEGORY"

CATEGORY PAGE URL PATTERNS (classify as "CATEGORY"):
- URLs containing: /productos/, /categoria/, /categories/, /collections/, /almacen/, /comestibles/
- URLs ending with category names: /harinas, /bebidas, /lacteos, /dulces, /carnes, etc.
- URLs like: domain.com/store/section/category or domain.com/productos/category-name
- Examples: elnaranjo.com.uy/productos/harinas-y-salvados, eldorado.com.uy/comestibles/almacen/harinas

ONLY classify as "PRODUCT" if:
- Page is dedicated to a single product FOR SALE
- Has specific product details, price, and purchase options
- Is an e-commerce product page where you can buy the item
- Example: "Columbia Impermeable Invierno Modelo XYZ" with price and buy button

This is critical so the price extractor only processes actual purchasable products.

CLASSIFICATION PRIORITY:
1. First check URL patterns (more reliable than snippets)
2. Then consider title and snippet content
3. If URL pattern clearly indicates CATEGORY, classify as "CATEGORY" even if snippet is unclear

Focus on the provided snippet, title, and URL.
URL: {url_info.url}
Title: {url_info.title}
Snippet: {url_info.snippet}
User's product query: "{product_name}"

Remember: Do NOT include any comments, explanations, or text outside or inside the JSON object. Do NOT use // or /* */ or any other comment syntax. Only output valid JSON.
"""
        user_prompt = f"Analyze the following web page information based on the user's query for '{product_name}':\nURL: {url_info.url}\nTitle: {url_info.title}\nSnippet: {url_info.snippet}\nReturn ONLY the JSON object as specified in the system instructions."

        response_text = ""
        cleaned_response_text = ""
        response_data = None

        try:
            async with OllamaClient() as llm:
                response_text = await llm.generate(
                    prompt=user_prompt,
                    system=system_prompt,
                    model=self.model_name,
                    temperature=self.temperature,
                    format="json"
                )
            logger.debug(f"LLM raw response for {url_info.url}: {response_text}")
            cleaned_response_text = strip_json_code_block(response_text)
            cleaned_response_text = remove_json_comments(cleaned_response_text)
            
            try: # Attempt 1: json.loads on the whole cleaned text
                response_data = json.loads(cleaned_response_text)
            except json.JSONDecodeError as main_jde: # If json.loads fails
                logger.warning(f"Initial JSONDecodeError for {url_info.url} ('{main_jde}'). Trying to parse first object with raw_decode.")
                try:
                    # Attempt 2: Try to parse only the first JSON object from the string
                    first_json_obj, end_index = json.JSONDecoder().raw_decode(cleaned_response_text)
                    
                    # Log if there was actually any significant trailing data
                    trailing_data = cleaned_response_text[end_index:].strip()
                    if trailing_data:
                        logger.warning(f"raw_decode for {url_info.url} successful, but found trailing data (first 200 chars): '{trailing_data[:200]}...'" )
                    else:
                        logger.info(f"raw_decode for {url_info.url} successful. No significant trailing data.")
                    response_data = first_json_obj # Use the successfully parsed first object
                except json.JSONDecodeError as raw_decode_jde:
                    # If raw_decode also fails, the original main_jde is more indicative of the problem
                    # with the initial part of the string. Log this failure and re-raise main_jde.
                    logger.error(f"raw_decode also failed for {url_info.url} after initial error. Raw_decode error: '{raw_decode_jde}'. Original cleaned text: {cleaned_response_text}")
                    raise main_jde from raw_decode_jde # Re-raise the original error to be caught by the outer handler
            
        except json.JSONDecodeError as jde: # Catches main_jde if raw_decode also failed or if json.loads failed for other reasons initially
            logger.warning(f"JSONDecodeError for {url_info.url}: {jde}. Attempting regex recovery...")
            
            # Try to extract page_type from partial/truncated JSON using regex
            recovered_data = extract_fields_from_partial_json(cleaned_response_text, ['page_type', 'identified_product_name', 'category_name', 'reasoning'])
            
            if recovered_data.get('page_type'):
                logger.info(f"Regex recovery successful for {url_info.url}: page_type={recovered_data.get('page_type')}")
                return IdentifiedPageCandidate(
                    url=url_info.url,
                    original_title=url_info.title,
                    original_snippet=url_info.snippet,
                    source_query=url_info.source_query,
                    page_type=recovered_data.get('page_type'),
                    reasoning=recovered_data.get('reasoning', 'Recovered from truncated response'),
                    identified_product_name=recovered_data.get('identified_product_name'),
                    category_name=recovered_data.get('category_name')
                )
            
            # Also try recovery from raw response (in case strip_json_code_block removed too much)
            recovered_data = extract_fields_from_partial_json(response_text, ['page_type', 'identified_product_name', 'category_name', 'reasoning'])
            if recovered_data.get('page_type'):
                logger.info(f"Regex recovery from raw response successful for {url_info.url}: page_type={recovered_data.get('page_type')}")
                return IdentifiedPageCandidate(
                    url=url_info.url,
                    original_title=url_info.title,
                    original_snippet=url_info.snippet,
                    source_query=url_info.source_query,
                    page_type=recovered_data.get('page_type'),
                    reasoning=recovered_data.get('reasoning', 'Recovered from truncated response'),
                    identified_product_name=recovered_data.get('identified_product_name'),
                    category_name=recovered_data.get('category_name')
                )
            
            logger.error(f"Final JSONDecodeError for {url_info.url}: {jde}. Recovery failed. Cleaned text was: {cleaned_response_text}")
            return IdentifiedPageCandidate(
                url=url_info.url,
                original_title=url_info.title,
                original_snippet=url_info.snippet,
                source_query=url_info.source_query,
                page_type="ERROR_PARSING_JSON",
                reasoning=f"Failed to parse LLM JSON response: {str(jde)}"
            )
        except httpx.HTTPStatusError as hse:
            logger.error(f"HTTPStatusError calling LLM for {url_info.url}: {hse.response.status_code} - {hse.response.text}", exc_info=True)
            return IdentifiedPageCandidate(
                url=url_info.url,
                original_title=url_info.title,
                original_snippet=url_info.snippet,
                source_query=url_info.source_query,
                page_type="ERROR_LLM_HTTP",
                reasoning=f"HTTPStatusError while calling LLM: {hse.response.status_code}"
            )
        except Exception as e_llm_comm: # Catch other errors during LLM communication/parsing
            logger.error(f"Unexpected error during LLM communication or JSON parsing for {url_info.url}: {e_llm_comm}", exc_info=True)
            return IdentifiedPageCandidate(
                url=url_info.url,
                original_title=url_info.title,
                original_snippet=url_info.snippet,
                source_query=url_info.source_query,
                page_type="ERROR_LLM_UNEXPECTED_COMM",
                reasoning=f"Unexpected error during LLM call or parsing: {str(e_llm_comm)}"
            )

        # If we've reached here, LLM call and JSON parsing were successful and response_data is populated.
        # Now, extract data and attempt to create IdentifiedPageCandidate.
        # Errors in this section (KeyError from .get, Pydantic ValidationError during construction) should propagate.

        page_type_from_llm = response_data.get("page_type")
        if page_type_from_llm is None:
            logger.warning(f"LLM response for {url_info.url} had null page_type. Defaulting to 'ERROR_LLM_NULL_PAGE_TYPE'. Raw response_data: {response_data}")
            final_page_type = "ERROR_LLM_NULL_PAGE_TYPE"
        else:
            final_page_type = str(page_type_from_llm)

        # Extract optional fields from LLM response
        llm_reasoning = response_data.get("reasoning")
        llm_identified_product_name = response_data.get("identified_product_name")
        llm_category_name = response_data.get("category_name")
            
        try:
            candidate = IdentifiedPageCandidate(
                # Fields from ExtractedUrlInfo
                url=url_info.url,
                original_title=url_info.title,
                original_snippet=url_info.snippet,
                source_query=url_info.source_query,

                # Fields from LLM
                page_type=final_page_type,
                reasoning=llm_reasoning,
                identified_product_name=llm_identified_product_name,
                category_name=llm_category_name
            )
            if self.page_type_cache_enabled and isinstance(candidate.page_type, str) and not candidate.page_type.startswith("ERROR_"):
                await self._set_cached_page_type(url_info.url, product_name, candidate.model_dump())
            return candidate
        except Exception as e_candidate_creation: # Catch Pydantic ValidationErrors or other issues
            logger.error(f"Critical error during IdentifiedPageCandidate creation for {url_info.url}: {e_candidate_creation}", exc_info=True)
            logger.error(f"Data for failing IdentifiedPageCandidate: url_info: {url_info.model_dump_json()}, llm_response_data: {response_data}")
            raise 

    async def identify_batch_page_types(
        self, 
        extracted_urls: List[ExtractedUrlInfo], 
        product_name: str,
        batch_size: int = 5,
        delay_between_batches: float = 0.01 # seconds
    ) -> List[IdentifiedPageCandidate]:
        identified_candidates: List[IdentifiedPageCandidate] = []
        if not extracted_urls:
            return identified_candidates
            
        for i in range(0, len(extracted_urls), batch_size):
            batch_of_url_info = extracted_urls[i:i+batch_size] # Renamed for clarity
            logger.info(f"Processing batch {i//batch_size + 1} of {(len(extracted_urls) + batch_size - 1)//batch_size} for page type identification.")
            
            tasks = [self._classify_url_with_llm(url_info, product_name) for url_info in batch_of_url_info]
            
            # Use return_exceptions=True to get exceptions as results instead of raising immediately
            results_or_exceptions = await asyncio.gather(*tasks, return_exceptions=True)
            
            for idx, res_or_exc in enumerate(results_or_exceptions):
                current_url_info = batch_of_url_info[idx] # Get corresponding url_info for context
                if isinstance(res_or_exc, Exception):
                    # This is an exception that was raised from _classify_url_with_llm 
                    # (e.g., Pydantic ValidationError or KeyError during IdentifiedPageCandidate creation)
                    logger.error(f"Exception for URL {current_url_info.url} in batch {i//batch_size + 1}: {res_or_exc}", exc_info=res_or_exc) # Log with traceback
                    identified_candidates.append(IdentifiedPageCandidate(
                        url=current_url_info.url,
                        original_title=current_url_info.title,
                        original_snippet=current_url_info.snippet,
                        source_query=current_url_info.source_query,
                        page_type="ERROR_CANDIDATE_INSTANTIATION",
                        reasoning=f"Failed during candidate object creation: {type(res_or_exc).__name__}"
                    ))
                elif isinstance(res_or_exc, IdentifiedPageCandidate): # This is a successfully created candidate or an error object returned by _classify_url_with_llm
                    identified_candidates.append(res_or_exc)
                else:
                    # Should not happen if _classify_url_with_llm always returns IdentifiedPageCandidate or raises Exception
                    logger.error(f"Unexpected result type for URL {current_url_info.url} in batch {i//batch_size + 1}: {type(res_or_exc)}", exc_info=True)
                    identified_candidates.append(IdentifiedPageCandidate(
                        url=current_url_info.url,
                        original_title=current_url_info.title,
                        original_snippet=current_url_info.snippet,
                        source_query=current_url_info.source_query,
                        page_type="ERROR_UNEXPECTED_RESULT_TYPE",
                        reasoning="Internal error: Unexpected result type from classification task."
                    ))
            
            if i + batch_size < len(extracted_urls):
                logger.debug(f"Waiting for {delay_between_batches}s before next batch.")
                await asyncio.sleep(delay_between_batches)
                
        logger.info(f"Identified page types for {len(identified_candidates)} URLs (may include error objects).")

        # Filter out error candidates (e.g., those with page_type starting with 'ERROR_')
        successful_candidates = []
        for candidate in identified_candidates:
            if hasattr(candidate, 'page_type') and isinstance(candidate.page_type, str) and candidate.page_type.startswith("ERROR_"):
                url = getattr(candidate, 'url', None)
                logger.warning(f"Skipping candidate for URL {url} due to error page_type: {candidate.page_type}")
            else:
                successful_candidates.append(candidate)

        logger.info(f"Returning {len(successful_candidates)} successfully identified page candidates (excluded {len(identified_candidates) - len(successful_candidates)} errors).")
        return successful_candidates 