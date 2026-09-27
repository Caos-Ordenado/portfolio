import json
from typing import List, Dict, Any
from shared.logging import setup_logger
from shared.llm_client import LLMClient, MODEL_EXTRACT
import re

logger = setup_logger("query_validator")

# System prompt to guide the LLM for validating search queries
QUERY_VALIDATOR_SYSTEM_PROMPT = """
You are a Search Query Validator AI. Your task is to analyze a list of search queries and determine if each query is valid based on the following criteria for finding a product in Uruguay/Montevideo:
- The query must clearly state purchase intent (e.g., using words like "comprar", "precio", "oferta", "tienda", "online", "adquirir", "descubre", "ecommerce", "tienda", "venta").
- The query should be specific enough to target a particular product or a narrow range of products, not overly broad categories if avoidable.
- The query should be well-formed and make sense in Spanish (Uruguayan context if applicable).
- The query should not be a question asking for information (e.g., "cuál es el mejor...") but rather a direct search term for finding items to buy.
- Avoid subjective terms like "mejor", "bueno", "barato" unless they are part of a very common buying phrase and don't make the query overly broad or unconfirmable for purchase intent.


IMPORTANT: Only validate queries that are likely to return individual product pages, not category or listing pages.
- Reject queries that are too generic or likely to return a list of products.
- Accept queries that are specific to a single product, model, or variant.
- Example of a valid query: "comprar impermeable invierno Columbia modelo XYZ en Montevideo"
- Example of an invalid query: "impermeables invierno Uruguay"

Input will be a JSON array of query strings.

Respond with a JSON array of objects. Each object must correspond to an input query and have the following structure:
{
  "query": "<original query string>",
  "valid": true_or_false,
  "reason": "<brief reason if not valid, empty string if valid>"
}

Ensure the output is ONLY the JSON array, with no other text or explanations.
"""

def strip_json_code_block(text: str) -> str:
    text = text.strip()
    
    # Regex to find content within ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.DOTALL)
    if match:
        # If found, return the stripped content of the first capturing group
        return match.group(1).strip()
    
    # Fallback: If no markdown code block is found,
    # try to extract content between the first '{' or '['
    # and the last '}' or ']'. This is a bit more fragile but
    # can work if the LLM omits the markdown fences.
    
    start_char_map = {'{': '}', '[': ']'}
    first_bracket_idx = text.find('[')
    first_curly_idx = text.find('{')
    
    start_idx = -1
    
    if first_bracket_idx != -1 and first_curly_idx != -1:
        start_idx = min(first_bracket_idx, first_curly_idx)
    elif first_bracket_idx != -1:
        start_idx = first_bracket_idx
    elif first_curly_idx != -1:
        start_idx = first_curly_idx
        
    if start_idx != -1:
        expected_end_char = start_char_map[text[start_idx]]
        # Find the last occurrence of the corresponding closing character
        end_idx = text.rfind(expected_end_char)
        
        if end_idx > start_idx:
            # Slice the string and strip whitespace
            return text[start_idx : end_idx+1].strip()
            
    # If no JSON structure is found by either method, return the original (stripped) text
    return text

class QueryValidatorAgent:
    def __init__(self, model_name=MODEL_EXTRACT, temperature=0.0):
        self.model_name = model_name
        self.temperature = temperature
        self.llm_client = LLMClient()
        logger.info(f"QueryValidatorAgent initialized with model: {model_name}, temp: {temperature}")

    async def __aenter__(self):
        logger.debug("QueryValidatorAgent context entered")
        await self.llm_client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("QueryValidatorAgent context exited")
        await self.llm_client.__aexit__(exc_type, exc_val, exc_tb)

    async def validate_queries(self, queries_to_validate: List[str]) -> List[Dict[str, Any]]:
        logger.info(f"Validating {len(queries_to_validate)} queries: {queries_to_validate}")
        
        if not queries_to_validate:
            return []

        queries_json_string = json.dumps(queries_to_validate, ensure_ascii=False)
        # Use system prompt for instructions, user prompt for queries
        user_prompt = f"Queries to validate (JSON array):\n{queries_json_string}"

        raw_llm_response = ""
        cleaned_response = "" # Initialize for use in except block
        try:
            raw_llm_response = await self.llm_client.generate(
                prompt=user_prompt,
                system=QUERY_VALIDATOR_SYSTEM_PROMPT,
                model=self.model_name,
                temperature=0.0,
                format="json"
            )
            logger.debug(f"Validator LLM raw response: {raw_llm_response}")
            cleaned_response = strip_json_code_block(raw_llm_response)
            logger.debug(f"Validator LLM cleaned response: {cleaned_response}")
            
            validation_data = json.loads(cleaned_response)
            
            if not isinstance(validation_data, list):
                logger.error("Validator LLM did not return a JSON array as expected.")
                return [{"query": q, "valid": False, "reason": "LLM response was not a list."} for q in queries_to_validate]

            final_validation_list = []
            for item in validation_data:
                if isinstance(item, dict) and "query" in item and "valid" in item:
                    # Core structure (query, valid) is present.
                    # Reason is optional; capture if present, otherwise it will be None.
                    processed_item = {
                        "query": item["query"],
                        "valid": item["valid"],
                        "reason": item.get("reason") # .get() returns None if 'reason' is not in item
                    }
                    final_validation_list.append(processed_item)
                else:
                    # Core structure (query/valid) is missing.
                    logger.warning(f"Invalid item structure in LLM validation response (missing query/valid): {item}. Marking related query as invalid.")
                    original_query = item.get("query", "Unknown query due to malformed LLM response") 
                    final_validation_list.append({
                        "query": original_query, 
                        "valid": False, 
                        "reason": "Malformed item in LLM response (missing query/valid keys)."
                    }) 
            
            # Ensure all original queries are accounted for, even if LLM missed some
            # or if items were malformed and didn't have a recoverable 'query' key.
            validated_queries_from_llm_set = {processed_item['query'] for processed_item in final_validation_list if isinstance(processed_item, dict) and 'query' in processed_item}
            
            for q_orig in queries_to_validate:
                if q_orig not in validated_queries_from_llm_set:
                    logger.warning(f"Original query '{q_orig}' not found in LLM's processed validation response. Marking as invalid.")
                    final_validation_list.append({"query": q_orig, "valid": False, "reason": "Query not found in LLM validation response or original item was too malformed."}) 
            
            return final_validation_list

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode validator LLM JSON response: {e}. Cleaned response was: {cleaned_response}")
            return [{"query": q, "valid": False, "reason": f"JSON decode error from LLM: {str(e)}"} for q in queries_to_validate]
        except Exception as e:
            logger.error(f"Error processing validation response: {e}. Raw response: {raw_llm_response}", exc_info=True)
            return [{"query": q, "valid": False, "reason": f"General processing error: {str(e)}"} for q in queries_to_validate] 