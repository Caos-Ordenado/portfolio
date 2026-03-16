"""
Geographic URL Validator Agent for filtering and validating URLs from geographically-targeted e-commerce sites.
"""

import re
import os
import hashlib
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse
from shared.ollama_client import OllamaClient
from shared.logging import setup_logger
from shared.redis_client import RedisClient


class GeoUrlValidatorAgent:
    """
    Agent responsible for validating URLs to ensure they are from geographically-targeted e-commerce sites.
    
    This agent integrates between Phase 2 (Web Search) and Phase 3 (URL Extraction) in the
    Product Search Agent workflow to filter out non-relevant geographic URLs and ensure localized results.
    """
    
    def __init__(self, llm_client: Optional[OllamaClient] = None, max_iterations: int = 3, target_url_count: int = 20, country: str = "UY", city: Optional[str] = None):
        """
        Initialize the Geographic URL Validator Agent.
        
        Args:
            llm_client: Optional OllamaClient instance. If None, will initialize default.
            max_iterations: Maximum number of retry iterations for search refinement.
            target_url_count: Target number of validated URLs to achieve.
            country: ISO country code (2-letter) or country name. Defaults to "UY" (Uruguay).
            city: Optional city name for more precise geographic filtering.
        """
        self.llm_client = llm_client or self._initialize_default_llm()
        self.max_iterations = max_iterations
        self.target_url_count = target_url_count
        self.logger = self._setup_logging()
        self.geo_cache_enabled = os.getenv("GEO_URL_CACHE_ENABLED", "true").lower() == "true"
        self.geo_cache_ttl_seconds = int(os.getenv("GEO_URL_CACHE_TTL_SECONDS", "604800"))  # 7 days
        
        # Validate and set geographic parameters
        self.country = self._validate_country(country)
        self.city = self._validate_city(city) if city else None
        
        # Initialize geographic patterns based on country
        self._initialize_geographic_patterns()
    
    def _validate_country(self, country: str) -> str:
        """
        Validate and normalize country code or name.
        
        Args:
            country: ISO country code (2-letter) or country name
            
        Returns:
            Normalized 2-letter country code
            
        Raises:
            ValueError: If country is invalid
        """
        # Dictionary of supported countries with ISO codes
        country_map = {
            "UY": "UY", "URUGUAY": "UY",
            "AR": "AR", "ARGENTINA": "AR", 
            "BR": "BR", "BRAZIL": "BR", "BRASIL": "BR",
            "CL": "CL", "CHILE": "CL",
            "CO": "CO", "COLOMBIA": "CO",
            "PE": "PE", "PERU": "PE",
            "EC": "EC", "ECUADOR": "EC",
            "MX": "MX", "MEXICO": "MX",
            "US": "US", "USA": "US", "UNITED STATES": "US",
            "ES": "ES", "SPAIN": "ES", "ESPAÑA": "ES"
        }
        
        country_upper = country.upper() if isinstance(country, str) else ""
        
        if country_upper in country_map:
            normalized = country_map[country_upper]
            self.logger.info(f"Country validated: {country} -> {normalized}")
            return normalized
        else:
            supported = ', '.join(sorted(set(country_map.values())))
            error_msg = f"Invalid country: {country}. Supported countries: {supported}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
    
    def _validate_city(self, city: str) -> Optional[str]:
        """
        Validate city name for the specified country.
        
        Args:
            city: City name to validate
            
        Returns:
            Normalized city name or None if no validation data
            
        Raises:
            ValueError: If city is invalid for the country
        """
        if not city:
            return None
            
        # Dictionary of supported cities by country
        city_map = {
            "UY": ["MONTEVIDEO", "PUNTA DEL ESTE", "COLONIA", "SALTO", "PAYSANDU", "MALDONADO"],
            "AR": ["BUENOS AIRES", "CORDOBA", "ROSARIO", "MENDOZA", "LA PLATA", "MAR DEL PLATA"],
            "BR": ["SAO PAULO", "RIO DE JANEIRO", "BRASILIA", "SALVADOR", "FORTALEZA", "BELO HORIZONTE"],
            "CL": ["SANTIAGO", "VALPARAISO", "CONCEPCION", "VINA DEL MAR", "ANTOFAGASTA", "TEMUCO"],
            "CO": ["BOGOTA", "MEDELLIN", "CALI", "BARRANQUILLA", "CARTAGENA", "BUCARAMANGA"],
            "PE": ["LIMA", "AREQUIPA", "TRUJILLO", "CHICLAYO", "PIURA", "CUSCO"],
            "EC": ["QUITO", "GUAYAQUIL", "CUENCA", "AMBATO", "MANTA", "MACHALA"],
            "MX": ["MEXICO CITY", "GUADALAJARA", "MONTERREY", "PUEBLA", "TIJUANA", "CANCUN"],
            "US": ["NEW YORK", "LOS ANGELES", "CHICAGO", "HOUSTON", "PHOENIX", "PHILADELPHIA"],
            "ES": ["MADRID", "BARCELONA", "VALENCIA", "SEVILLA", "ZARAGOZA", "MALAGA"]
        }
        
        city_upper = city.upper() if isinstance(city, str) else ""
        
        # If country not in city_map, allow any city (lenient validation)
        if self.country not in city_map:
            self.logger.info(f"City accepted (no validation data): {city} for country {self.country}")
            return city.title()
            
        # Validate against known cities for this country
        if city_upper in city_map[self.country]:
            self.logger.info(f"City validated: {city} in {self.country}")
            return city.title()
        else:
            supported = ', '.join(city_map[self.country])
            error_msg = f"Invalid city: {city} for country {self.country}. Supported cities: {supported}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
    
    def _initialize_geographic_patterns(self):
        """Initialize domain patterns and known sites based on the country."""
        # Map of country codes to their TLDs
        country_tlds = {
            "UY": ['.uy', '.com.uy', '.edu.uy', '.gub.uy', '.org.uy', '.net.uy'],
            "AR": ['.ar', '.com.ar', '.edu.ar', '.gob.ar', '.org.ar', '.net.ar'],
            "BR": ['.br', '.com.br', '.edu.br', '.gov.br', '.org.br', '.net.br'],
            "CL": ['.cl', '.com.cl', '.edu.cl', '.gob.cl', '.org.cl', '.net.cl'],
            "CO": ['.co', '.com.co', '.edu.co', '.gov.co', '.org.co', '.net.co'],
            "PE": ['.pe', '.com.pe', '.edu.pe', '.gob.pe', '.org.pe', '.net.pe'],
            "EC": ['.ec', '.com.ec', '.edu.ec', '.gob.ec', '.org.ec', '.net.ec'],
            "MX": ['.mx', '.com.mx', '.edu.mx', '.gob.mx', '.org.mx', '.net.mx'],
            "US": ['.us', '.com', '.edu', '.gov', '.org', '.net'],
            "ES": ['.es', '.com.es', '.edu.es', '.gob.es', '.org.es', '.net.es']
        }
        
        # Map of country codes to known e-commerce domains
        country_domains = {
            "UY": [
                'mercadolibre.com.uy', 'tiendainglesa.com.uy', 'devoto.com.uy',
                'farmacity.com.uy', 'disco.com.uy', 'geant.com.uy',
                'zonaamerica.com', 'puntashop.com', 'lider.com.uy'
            ],
            "AR": [
                'mercadolibre.com.ar', 'pedidosya.com.ar', 'tiendamia.com.ar',
                'falabella.com.ar', 'garbarino.com', 'fravega.com'
            ],
            "BR": [
                'mercadolivre.com.br', 'americanas.com.br', 'submarino.com.br',
                'magazineluiza.com.br', 'casasbahia.com.br', 'extra.com.br'
            ],
            "CL": [
                'mercadolibre.cl', 'falabella.com', 'ripley.cl',
                'lider.cl', 'paris.cl', 'sodimac.cl'
            ],
            "CO": [
                'mercadolibre.com.co', 'falabella.com.co', 'exito.com',
                'alkosto.com', 'linio.com.co', 'homecenter.com.co'
            ],
            "PE": [
                'mercadolibre.com.pe', 'falabella.com.pe', 'ripley.com.pe',
                'wong.pe', 'tottus.com.pe', 'plazavea.com.pe'
            ],
            "EC": [
                'mercadolibre.com.ec', 'de-una.com', 'megamaxi.com',
                'supermaxi.com', 'tia.com.ec', 'comandato.com'
            ],
            "MX": [
                'mercadolibre.com.mx', 'amazon.com.mx', 'liverpool.com.mx',
                'elektra.com.mx', 'coppel.com', 'soriana.com'
            ],
            "US": [
                'amazon.com', 'walmart.com', 'target.com',
                'bestbuy.com', 'homedepot.com', 'lowes.com'
            ],
            "ES": [
                'amazon.es', 'elcorteingles.es', 'carrefour.es',
                'mediamarkt.es', 'worten.es', 'fnac.es'
            ]
        }
        
        # Set patterns for the current country
        self.country_domains = set(country_tlds.get(self.country, ['.com']))  # fallback to .com
        self.known_sites = set(country_domains.get(self.country, []))
        
        self.logger.info(f"Initialized geographic patterns for {self.country}: "
                        f"{len(self.country_domains)} TLDs, {len(self.known_sites)} known sites")
    
    def _initialize_default_llm(self) -> OllamaClient:
        """
        Initialize the default LLM client with fallback model selection.
        
        Returns:
            OllamaClient: Configured client instance.
        """
        return OllamaClient(model="qwen3:latest")
    
    def _setup_logging(self):
        """
        Setup logging for the Geographic URL Validator Agent.
        
        Returns:
            Logger: Configured logger instance.
        """
        return setup_logger("geo_url_validator")
    
    async def validate_urls(self, urls: List[str], search_query: str) -> List[str]:
        """
        Main method to validate URLs for geographic relevance.
        
        Args:
            urls: List of URLs to validate.
            search_query: Original search query for context.
            
        Returns:
            List[str]: Filtered list of valid URLs for the target country/city.
        """
        if not urls:
            self.logger.warning("Empty URL list provided for validation")
            return []
            
        location_desc = f"{self.country}" + (f"/{self.city}" if self.city else "")
        self.logger.info(f"Validating {len(urls)} URLs for {location_desc} relevance")
        
        validated_urls = []
        cached_false = set()
        remaining_urls = urls

        cache_client = None
        if self.geo_cache_enabled:
            try:
                cache_client = RedisClient()
                await cache_client.__aenter__()
                if not await cache_client.health_check():
                    await cache_client.__aexit__(None, None, None)
                    cache_client = None
            except Exception:
                cache_client = None

        if self.geo_cache_enabled and cache_client:
            remaining_urls = []
            for url in urls:
                cached = await self._get_cached_geo(url, cache_client)
                if cached is True:
                    validated_urls.append(url)
                elif cached is False:
                    cached_false.add(url)
                else:
                    remaining_urls.append(url)
        
        # First pass: Domain and path pattern matching
        for url in remaining_urls:
            try:
                if self._is_country_domain(url) or self._has_country_path_indicators(url):
                    validated_urls.append(url)
                    self.logger.debug(f"URL passed domain/path validation: {url}")
            except Exception as e:
                self.logger.error(f"Error validating URL {url}: {e}")
                continue
        
        # Second pass: LLM-based contextual validation for ambiguous URLs
        remaining_urls = [url for url in remaining_urls if url not in validated_urls and url not in cached_false]
        if remaining_urls:
            self.logger.info(f"Performing LLM validation on {len(remaining_urls)} remaining URLs")
            llm_validated = await self._llm_validate_urls(remaining_urls, search_query)
            validated_urls.extend(llm_validated)
        
        # Remove duplicates while preserving order
        final_urls = list(dict.fromkeys(validated_urls))

        if self.geo_cache_enabled and cache_client:
            for url in urls:
                await self._set_cached_geo(url, url in final_urls, cache_client)
            await cache_client.__aexit__(None, None, None)
        
        self.logger.info(f"Validation complete: {len(final_urls)}/{len(urls)} URLs validated as {self.country}-relevant")
        
        return final_urls

    def _geo_cache_key(self, url: str) -> str:
        digest = hashlib.sha256(f"{self.country}:{url}".encode("utf-8")).hexdigest()
        return f"geo_ok:{self.country}:{digest}"

    async def _get_cached_geo(self, url: str, redis_client: RedisClient) -> Optional[bool]:
        try:
            value = await redis_client.get(self._geo_cache_key(url))
            if value is None:
                return None
            if isinstance(value, str):
                return value == "1"
            return bool(value)
        except Exception:
            return None

    async def _set_cached_geo(self, url: str, is_valid: bool, redis_client: RedisClient) -> None:
        try:
            await redis_client.set(
                self._geo_cache_key(url),
                "1" if is_valid else "0",
                ex=self.geo_cache_ttl_seconds,
            )
        except Exception:
            return
    
    async def _regenerate_search_query(self, original_query: str) -> str:
        """
        Method to enhance query with geographic-specific terms.
        
        Args:
            original_query: The original search query.
            
        Returns:
            str: Enhanced query with country/city-specific terms.
        """
        if not original_query or not original_query.strip():
            self.logger.warning("Empty or invalid search query provided")
            return original_query
            
        # Check if query already contains geographic terms
        geographic_terms = self._get_search_terms()
        query_lower = original_query.lower()
        
        has_geographic_terms = any(term in query_lower for term in geographic_terms)
        
        if has_geographic_terms:
            location_desc = f"{self.country}" + (f"/{self.city}" if self.city else "")
            self.logger.info(f"Query already contains {location_desc} terms: {original_query}")
            return original_query
            
        # Create location context for LLM
        location_context = f"country {self.country}"
        if self.city:
            location_context = f"city {self.city} in {location_context}"
            
        # Use LLM to enhance the query with geographic terms
        prompt = f"""You are a search query optimizer for e-commerce in {location_context}.

Task: Enhance the following search query to focus on retailers and local market in {location_context}.

Original Query: "{original_query}"

Guidelines:
1. Add location-specific terms for {self.country}{f" and {self.city}" if self.city else ""}
2. Include relevant purchase intent keywords appropriate for the region
3. Keep the original product intent clear
4. Make it natural and search-engine friendly
5. Generate 1-3 enhanced queries

Respond with ONLY a JSON array of enhanced queries, like: ["enhanced query 1", "enhanced query 2"]"""

        try:
            async with self.llm_client as llm:
                response = await llm.generate(
                    prompt=prompt,
                    temperature=0.5,  # Medium creativity for query variation
                    num_predict=150,   # Limit tokens for concise responses
                    format="json"
                )
                
                # Parse LLM response
                import json
                response_text = response.strip()
                
                # Handle potential formatting issues
                if response_text.startswith('```json'):
                    response_text = response_text.replace('```json', '').replace('```', '').strip()
                elif response_text.startswith('```'):
                    response_text = response_text.replace('```', '').strip()
                
                enhanced_queries = json.loads(response_text)
                
                if enhanced_queries and isinstance(enhanced_queries, list):
                    # Return the first enhanced query (best option)
                    enhanced_query = enhanced_queries[0]
                    self.logger.info(f"Enhanced query: '{original_query}' -> '{enhanced_query}'")
                    return enhanced_query
                else:
                    self.logger.warning("LLM returned invalid query format, using fallback")
                    return self._fallback_enhance_query(original_query)
                    
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse LLM response as JSON: {e}")
            return self._fallback_enhance_query(original_query)
        except Exception as e:
            self.logger.error(f"Error during LLM query enhancement: {e}")
            return self._fallback_enhance_query(original_query)
    
    def _fallback_enhance_query(self, original_query: str) -> str:
        """
        Fallback method to enhance query when LLM fails.
        
        Args:
            original_query: The original search query.
            
        Returns:
            str: Enhanced query using simple pattern-based enhancement.
        """
        # Simple enhancement by adding Uruguay terms
        enhanced_options = [
            f"{original_query} Uruguay",
            f"comprar {original_query} Uruguay",
            f"{original_query} Montevideo",
            f"{original_query} tienda Uruguay"
        ]
        
        # Return the first option as default
        enhanced_query = enhanced_options[0]
        self.logger.info(f"Fallback enhanced query: '{original_query}' -> '{enhanced_query}'")
        return enhanced_query
    
    def _is_country_domain(self, url: str) -> bool:
        """
        Check if URL belongs to the target country domain based on domain patterns.
        
        Args:
            url: URL to check.
            
        Returns:
            bool: True if URL is from the target country domain.
        """
        try:
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()
            
            # Check for country-specific TLDs
            for country_domain in self.country_domains:
                if domain.endswith(country_domain):
                    return True
            
            # Check for known sites in this country
            if domain in self.known_sites:
                return True
                
            # Check for country-related subdomains (basic pattern matching)
            country_indicators = self._get_country_indicators()
            for indicator in country_indicators:
                if indicator in domain:
                    return True
                    
            return False
            
        except Exception as e:
            self.logger.warning(f"Error parsing URL {url}: {e}")
            return False
    
    def _get_country_indicators(self) -> List[str]:
        """Get domain indicators for the current country."""
        country_indicators = {
            "UY": ['uruguay', 'uy', 'montevideo'],
            "AR": ['argentina', 'ar', 'buenosaires', 'argentina.com'],
            "BR": ['brasil', 'brazil', 'br', 'saopaulo'],
            "CL": ['chile', 'cl', 'santiago'],
            "CO": ['colombia', 'co', 'bogota'],
            "PE": ['peru', 'pe', 'lima'],
            "EC": ['ecuador', 'ec', 'quito'],
            "MX": ['mexico', 'mx', 'mexicocity'],
            "US": ['usa', 'us', 'america'],
            "ES": ['espana', 'spain', 'es', 'madrid']
        }
        return country_indicators.get(self.country, [self.country.lower()])
    
    def _get_search_terms(self) -> List[str]:
        """Get search terms for query enhancement for the current country/city."""
        search_terms = {
            "UY": ['uruguay', 'uy', 'montevideo', 'maldonado', 'punta del este', 'canelones'],
            "AR": ['argentina', 'ar', 'buenos aires', 'bsas', 'cordoba', 'rosario'],
            "BR": ['brasil', 'brazil', 'br', 'sao paulo', 'rio', 'brasil.com'],
            "CL": ['chile', 'cl', 'santiago', 'valparaiso', 'concepcion'],
            "CO": ['colombia', 'co', 'bogota', 'medellin', 'cali'],
            "PE": ['peru', 'pe', 'lima', 'arequipa', 'trujillo'],
            "EC": ['ecuador', 'ec', 'quito', 'guayaquil', 'cuenca'],
            "MX": ['mexico', 'mx', 'ciudad mexico', 'guadalajara', 'monterrey'],
            "US": ['usa', 'us', 'america', 'new york', 'los angeles'],
            "ES": ['espana', 'spain', 'es', 'madrid', 'barcelona']
        }
        
        base_terms = search_terms.get(self.country, [self.country.lower()])
        
        # Add city-specific terms if city is specified
        if self.city:
            base_terms.append(self.city.lower())
            
        return base_terms
    
    def _has_country_path_indicators(self, url: str) -> bool:
        """
        Check if URL path contains country-related indicators.
        
        Args:
            url: URL to check.
            
        Returns:
            bool: True if URL path contains country indicators.
        """
        try:
            parsed_url = urlparse(url)
            path = parsed_url.path.lower()
            query = parsed_url.query.lower()
            
            # Generate country-specific path indicators
            path_indicators = self._get_path_indicators()
            
            for indicator in path_indicators:
                if indicator in path or indicator in query:
                    return True
                    
            return False
            
        except Exception as e:
            self.logger.warning(f"Error checking path indicators for URL {url}: {e}")
            return False
    
    def _get_path_indicators(self) -> List[str]:
        """Get path indicators for the current country and city."""
        path_indicators = {
            "UY": ['/uruguay/', '/uy/', '/montevideo/', 'country=uy', 'region=uruguay'],
            "AR": ['/argentina/', '/ar/', '/buenosaires/', 'country=ar', 'region=argentina'],
            "BR": ['/brasil/', '/brazil/', '/br/', '/saopaulo/', 'country=br', 'region=brasil'],
            "CL": ['/chile/', '/cl/', '/santiago/', 'country=cl', 'region=chile'],
            "CO": ['/colombia/', '/co/', '/bogota/', 'country=co', 'region=colombia'],
            "PE": ['/peru/', '/pe/', '/lima/', 'country=pe', 'region=peru'],
            "EC": ['/ecuador/', '/ec/', '/quito/', 'country=ec', 'region=ecuador'],
            "MX": ['/mexico/', '/mx/', '/mexicocity/', 'country=mx', 'region=mexico'],
            "US": ['/usa/', '/us/', '/america/', 'country=us', 'region=usa'],
            "ES": ['/espana/', '/spain/', '/es/', '/madrid/', 'country=es', 'region=spain']
        }
        
        base_indicators = path_indicators.get(self.country, [f'/{self.country.lower()}/'])
        
        # Add city-specific indicators if city is specified
        if self.city:
            city_indicators = [
                f'/{self.city.lower()}/',
                f'city={self.city.lower()}',
                f'location={self.city.lower()}'
            ]
            base_indicators.extend(city_indicators)
            
        return base_indicators
    
    async def _llm_validate_urls(self, urls: List[str], search_query: str) -> List[str]:
        """
        Use LLM to validate URLs that didn't pass domain/path checks.
        
        Args:
            urls: List of URLs to validate with LLM.
            search_query: Original search query for context.
            
        Returns:
            List[str]: URLs validated as geographically-relevant by LLM.
        """
        if not urls:
            return []
            
        # Create geographic context description
        location_context = f"country {self.country}"
        if self.city:
            location_context = f"city {self.city} in {location_context}"
            
        # Create URLs list for the prompt
        urls_text = "\n".join([f"- {url}" for url in urls])
        
        # Use system prompt for instructions and user prompt for the task
        system_prompt = f"""You are a STRICT URL classifier for {location_context} e-commerce validation.

TASK: Return ONLY URLs from {location_context} domains that serve local customers.

RESPONSE FORMAT: Valid JSON array only. No explanations, no markdown, no additional text.
- If URLs match criteria: ["url1", "url2"]
- If NO URLs match criteria: []
- NEVER return error messages or explanations

STRICT CRITERIA - INCLUDE ONLY if domain meets ONE of these:
1. Ends with .{self.country.lower()} (like example.{self.country.lower()})
2. Ends with .com.{self.country.lower()} (like example.com.{self.country.lower()})
3. Contains "{self.country.lower()}" directly in domain name (like {self.country.lower()}shop.com)

EXCLUDE ALL:
- .com domains WITHOUT {self.country.lower()} in domain name
- Domains from other countries (.com.ar, .com.br, .com.co, .com.pe, .cl, .mx)
- International sites (.com, .org, .net) unless domain name contains "{self.country.lower()}"

EXAMPLES TO EXCLUDE: amazon.com, semillabreadshop.com, plazavea.com.pe, mercadolibre.com.ar

Return ONLY the JSON array. If no URLs qualify, return []."""

        user_prompt = f"""Search Query: "{search_query}"

URLs to classify for {location_context}:
{urls_text}

Return only the JSON array of URLs that serve {location_context}:"""

        try:
            # Debug log the prompts being sent to LLM
            # self.logger.debug(f"LLM validation system prompt: {system_prompt}")
            # self.logger.debug(f"LLM validation user prompt: {user_prompt}")
            
            async with self.llm_client as llm:
                response = await llm.generate(
                    prompt=user_prompt,
                    system=system_prompt,
                    temperature=0.0,  # Zero temperature for maximum determinism
                    num_predict=200,   # Reduced for concise JSON-only responses
                    format="json"
                )
                
                # Parse LLM response
                import json
                response_text = (response or "").strip()

                if not response_text:
                    self.logger.warning("LLM returned empty response for URL validation")
                    return []

                self.logger.debug(f"Raw LLM response for URL validation: {response_text}")

                # Strip markdown fences
                if response_text.startswith('```json'):
                    response_text = response_text[len('```json'):].strip()
                    if response_text.endswith('```'):
                        response_text = response_text[:-3].strip()
                elif response_text.startswith('```'):
                    response_text = response_text[3:].strip()
                    if response_text.endswith('```'):
                        response_text = response_text[:-3].strip()

                # Check if LLM returned an error object instead of array
                if response_text.startswith('{') and '"error"' in response_text:
                    try:
                        error_obj = json.loads(response_text)
                        if isinstance(error_obj, dict) and "error" in error_obj:
                            self.logger.warning(f"LLM returned error object: {error_obj['error']}. Treating as empty result.")
                            return []
                    except json.JSONDecodeError:
                        pass
                
                # Extract first JSON array between [ and ]
                start_idx = response_text.find('[')
                end_idx = response_text.rfind(']')
                if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                    self.logger.error("No JSON array found in LLM response after cleaning")
                    return []
                cleaned_json = response_text[start_idx:end_idx + 1]

                try:
                    validated_urls = json.loads(cleaned_json)
                except json.JSONDecodeError as e:
                    self.logger.error(f"Failed to parse cleaned JSON array: {e}. Cleaned: {cleaned_json[:200]}")
                    return []
                
                # Ensure response is a list
                if not isinstance(validated_urls, list):
                    self.logger.warning(f"LLM response is not a list: {type(validated_urls)}. Response: {validated_urls}")
                    return []
                
                # Ensure all returned URLs were in the original list
                valid_responses = [url for url in validated_urls if url in urls]
                
                # CRITICAL: Add programmatic safety check to filter out obvious foreign domains
                foreign_domains = ['.mx', '.pe', '.ar', '.br', '.cl', '.co', '.cr', '.es', '.pt']
                final_responses = []
                for url in valid_responses:
                    url_lower = url.lower()
                    is_foreign = any(url_lower.endswith(domain) or f"{domain}/" in url_lower for domain in foreign_domains)
                    if is_foreign:
                        self.logger.warning(f"SAFETY: Filtering out foreign URL that LLM incorrectly validated: {url}")
                    else:
                        final_responses.append(url)
                
                self.logger.info(f"LLM validated {len(valid_responses)} URLs, safety filter passed {len(final_responses)} URLs as {self.country}-relevant")
                return final_responses
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse LLM response as JSON: {e}. Raw response: '{response_text if 'response_text' in locals() else 'N/A'}'")
            return []
        except Exception as e:
            self.logger.error(f"Error during LLM URL validation: {e}")
            return [] 