import json
from shared.logging import setup_logger
from shared.llm_client import LLMClient, MODEL_EXTRACT
from shared.utils import strip_json_code_block, fix_truncated_json
from shared.utils.json_utils import extract_queries_with_regex

logger = setup_logger("query_generator")

# System prompt to guide the LLM for generating search queries
SYSTEM_PROMPT = """
You are a Web Search Query Generator AI. Your task is to generate 5 optimized search queries for finding a specific product online, focusing on purchase intent in Uruguay, specifically Montevideo. The queries should be suitable for e-commerce sites and general web search engines.

Requirements:
1. Generate exactly 5 distinct queries.
2. **Purchase Intent**: include intent keywords such as "comprar", "precio", "oferta", "tienda", "online"
3. **Product Clarity**:Iinsert the provided product name verbatim.
4. **Location**: include "Montevideo" and/or "Uruguay" in at least 4 of the 5 queries.
5. **Natural phrasing**: emulate how local shoppers search; vary structure (e.g. brand + product, product + feature, generic product type + location).
6. Avoid overly technical terms unless part of the product name.
7. **Format**:
    - Output ONLY a JSON array of 5 strings.
    - No surrounding text, line breaks, or comments.

IMPORTANT: 
- Your goal is to generate search queries that will return individual product pages, not category, collection, or listing pages. 
- Each query should be as specific as possible, including product model, brand, or unique features if available.
- If the product has a unique identifier (SKU, model number, price), include it in the query.
- Avoid generic queries that would return a list of products or a category page.


Example of a good query: "comprar impermeable invierno Uruguay"
Example of a good query: "tienda bicicleta GT 2025 Montevideo"
Example of a bad query: "impermeables invierno" (too broad, likely to return category pages, not related to Uruguay)


Return a JSON array of 5 strings, with each string being a search query.
Ensure the output is ONLY the JSON array, with no other text or explanations.
Ensure that each query string within the JSON array is a single line and does not contain any newline characters (e.g., \n).
"""


class QueryGeneratorAgent:
    def __init__(self, model_name=MODEL_EXTRACT, temperature=0.1):
        self.model_name = model_name
        self.temperature = temperature
        self.llm_client = LLMClient()
        logger.info(f"QueryGeneratorAgent initialized with model: {model_name}, temp: {temperature}")

    async def __aenter__(self):
        logger.debug("QueryGeneratorAgent context entered")
        await self.llm_client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("QueryGeneratorAgent context exited")
        await self.llm_client.__aexit__(exc_type, exc_val, exc_tb)

    async def generate_queries(self, product: str):
        logger.info(f"Generating queries for product: {product}")
        
        # Use system prompt for instructions, user prompt for product
        user_prompt = f"Product: {product}"

        raw_llm_response = ""
        clean_response = ""
        try:
            raw_llm_response = await self.llm_client.generate(
                prompt=user_prompt,
                system=SYSTEM_PROMPT,
                model=self.model_name,
                temperature=0.0,
                format="json"
            )
            logger.debug(f"LLM response: {raw_llm_response}")
            
            clean_response = strip_json_code_block(raw_llm_response)
            queries = self._parse_queries_response(clean_response)
            
            if queries:
                return queries, raw_llm_response
            else:
                raise ValueError("No queries extracted from response")

        except json.JSONDecodeError as jde:
            logger.warning(f"JSONDecodeError in generate_queries for product {product}: {jde}. Attempting recovery...")
            
            # Recovery attempt 1: Try to fix truncated JSON
            try:
                fixed_response = fix_truncated_json(clean_response)
                queries = self._parse_queries_response(fixed_response)
                if queries:
                    logger.info(f"Recovery successful: extracted {len(queries)} queries after fixing truncated JSON")
                    return queries, raw_llm_response
            except Exception as fix_err:
                logger.debug(f"Truncated JSON fix failed: {fix_err}")
            
            # Recovery attempt 2: Extract queries with regex
            queries = extract_queries_with_regex(raw_llm_response)
            if queries:
                logger.info(f"Regex recovery successful: extracted {len(queries)} queries")
                return queries, raw_llm_response
            
            logger.error(f"All recovery attempts failed for product {product}. Raw response: {raw_llm_response}")
            return [], raw_llm_response
            
        except Exception as e:
            logger.error(f"Error generating queries for product {product}: {e}. Raw response: {raw_llm_response}", exc_info=True)
            
            # Try regex extraction as last resort
            queries = extract_queries_with_regex(raw_llm_response)
            if queries:
                logger.info(f"Regex fallback recovered {len(queries)} queries after error")
                return queries, raw_llm_response
            
            return [], raw_llm_response
    
    def _parse_queries_response(self, text: str) -> list:
        """Parse queries from a JSON response, handling various formats."""
        parsed = json.loads(text)

        # Accept either a raw JSON array or an object with { "queries": [...] }
        if isinstance(parsed, dict) and "queries" in parsed and isinstance(parsed["queries"], list):
            queries = parsed["queries"]
        elif isinstance(parsed, list):
            queries = parsed
        elif isinstance(parsed, str):
            # Sometimes models wrap JSON in a string; try one more parse
            potential_list = json.loads(parsed)
            if isinstance(potential_list, list):
                queries = potential_list
            elif isinstance(potential_list, dict) and isinstance(potential_list.get("queries"), list):
                queries = potential_list["queries"]
            else:
                raise ValueError("LLM did not return a JSON array or {queries: [...]}.")
        else:
            raise ValueError("LLM did not return a JSON array or {queries: [...]}.")
        
        # Filter to only valid string queries
        if not all(isinstance(q, str) for q in queries):
            logger.warning(f"Response contained non-string elements: {queries}. Filtering.")
            queries = [q for q in queries if isinstance(q, str)]
        
        return queries