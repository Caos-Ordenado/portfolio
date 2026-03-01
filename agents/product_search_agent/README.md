# Product Search Agent

## Overview
A FastAPI-based agent that automates the discovery and classification of e-commerce product pages based on natural language product queries. The agent leverages AI-powered query generation, web search, intelligent URL validation, and machine learning classification to provide highly relevant product results for any specified geographic market.

## Key Features
- **AI-Enhanced Query Generation**: Converts simple product names into optimized search queries with purchase intent
- **Geographic URL Validation**: Filters URLs for specified country/city relevance using AI and pattern matching
- **Multi-Country Support**: Configurable for UY, AR, BR, CL, CO, PE, EC, MX, US, ES markets
- **Intelligent Product Page Discovery**: Automatically identifies and classifies potential product pages
- **Proactive Web Crawling**: Triggers comprehensive content extraction for deeper product analysis
- **Market-Adaptable**: Dynamically adapts to different geographic e-commerce landscapes

## Workflow Phases

### Phase 1: Query Generation
- Converts user input into optimized search queries
- Uses LLM to generate 5 targeted queries with purchase intent
- Adapts to specified country/city market terminology and language

### Phase 2: Web Search Execution
- Executes parallel searches using Brave Search API
- Aggregates results from all generated queries
- Collects URLs, titles, descriptions, and snippets

### Phase 2.5: URL Validation
The workflow includes a dedicated validation phase to ensure geographic relevance:

1. **Input**: Raw search results from Phase 2 (Web Search)
2. **Process**:
   - Initial domain filtering (country-specific TLDs and domains)
   - LLM-based contextual analysis with geographic prompts
   - Business name pattern matching for local retailers
   - Geographic relevance scoring based on country/city parameters
3. **Output**: Filtered list of geographically-relevant URLs

This phase ensures that downstream processing focuses only on geographically relevant results, improving overall accuracy and efficiency. If no Uruguay-relevant URLs remain, the pipeline stops (no fail-open fallback).

### Phase 3: URL Extraction and Pre-filtering
- **3-Stage Pre-filtering Pipeline**: Pattern-based filtering → Advanced duplicate detection → LLM bulk classification
- **Pattern Filtering**: Excludes navigation, auth, and non-product URLs using 22+ exclusion patterns
- **Domain Rate Limiting**: Max 8 URLs per domain to prevent over-representation
- **Performance**: 50-90% URL reduction before expensive downstream processing
- Triggers proactive web crawling for filtered results

### Phase 4: Proactive Web Crawling
- Initiates asynchronous crawling via Web Crawler Service
- Configurable parameters for depth and concurrency

### Phase 5: Product Page Classification
- Uses AI to classify URLs as product pages
- Provides confidence scoring for results

### Relevance Scoring and Montevideo Preference
- After page classification, candidates are scored for **query relevance** and **Montevideo preference**
- Uruguay is a strict filter; Montevideo is a **ranking signal**, not a hard exclusion
- Only the top candidates (controlled by `MAX_PRODUCT_CANDIDATES_FOR_PRICE`) proceed to price extraction
- `MIN_RELEVANCE_SCORE` filters out low-match pages before ranking (falls back to top-ranked if none pass)

### Price Extraction Robustness
- If crawler/renderer text is missing or too short, the agent attempts a **vision-based fallback**
- Controlled by `PRICE_VISION_ON_NO_TEXT` (default: true)

### Caching (Derived Artifacts)
In addition to search results (`search_results:*`) and page content (`webpage:*`), the agent caches:
- **Geo decisions**: `geo_ok:{country}:{sha(url)}` (TTL: `GEO_URL_CACHE_TTL_SECONDS`, default 7d)
- **Page type**: `page_type:{sha(url|query)}` (TTL: `PAGE_TYPE_CACHE_TTL_SECONDS`, default 6h)
- **Relevance score**: `relevance:{sha(url|query)}` (TTL: `RELEVANCE_CACHE_TTL_SECONDS`, default 6h)
- **Price results**: `price:{sha(url)}` (TTL: `PRICE_CACHE_TTL_SECONDS`, default 2h)

### Security Logging
- Database/Redis passwords are **redacted** in logs by default
- Set `LOG_SENSITIVE_CONFIG=true` only for local debugging (avoid in production)

## GeoUrlValidatorAgent

The GeoUrlValidatorAgent is responsible for validating search result URLs to ensure they are relevant to the specified geographic location (country and optionally city). This agent operates in Phase 2.5 of the workflow, between web search execution and URL extraction.

### Features
- Domain pattern matching for country-specific TLDs (e.g., .uy, .com.uy for Uruguay; .ar, .com.ar for Argentina)
- LLM-based contextual analysis using the deepseek-r1:1.5b model with geographic prompts
- Business name pattern recognition for local entities per country
- Geographic indicator detection for specified country/city
- Multi-country support: UY, AR, BR, CL, CO, PE, EC, MX, US, ES
- Multi-language support: Spanish, Portuguese, English (automatic detection)
- Automatic retry logic for insufficient results

### Model Integration

The validator uses the deepseek-r1:1.5b model for contextual analysis of URLs. This lightweight model was chosen for its:
- Low latency (average response time < 500ms)
- Efficient resource usage
- Strong performance on geographic entity recognition
- Multi-language support (Spanish, Portuguese, English)
- Adaptable prompting for different countries and cultures

```python
# Example configuration
from src.core.geo_url_validator_agent import GeoUrlValidatorAgent

# For Uruguay (default)
validator = GeoUrlValidatorAgent(
    country="UY",
    city="Montevideo",      # Optional city parameter
    target_url_count=20,    # Minimum URLs to collect
    max_iterations=3        # Maximum retry attempts
)

# For Argentina
validator = GeoUrlValidatorAgent(
    country="AR",
    city="Buenos Aires",
    target_url_count=20,
    max_iterations=3
)
```

### Retry Logic

When fewer than the target number of geographically-relevant URLs are found (default: 20), the agent implements an automatic retry mechanism:

1. Generates refined search queries with stronger geographic context for the specified country
2. Executes additional search iterations (up to 3 by default)
3. Progressively adds more explicit country and city-specific terms in the local language
4. Maintains a cumulative list of validated URLs across iterations

```python
# Example configuration
validator = GeoUrlValidatorAgent(
    country="BR",             # Brazil market
    city="São Paulo",         # Optional city
    target_url_count=20,      # Minimum URLs to collect
    max_iterations=3,         # Maximum retry attempts
)
```

### Performance Considerations

The URL validation phase is optimized for performance:
- Batch processing of URLs to minimize LLM API calls
- Parallel domain pattern matching for quick filtering
- Adaptive LLM batch sizing based on available resources
- Average processing time: < 2 seconds per batch
- Graceful degradation with fallback to pattern-only validation

## API Endpoints

### GET /search
- **Query params:** 
  - `product` (str, required): Product to search for
  - `country` (str, optional): Country code (default: "UY")
  - `city` (str, optional): City name for more specific validation
- **Response:**
  ```json
  {
    "success": true,
    "query": "crema para el cabello",
    "generated_queries": [
      "comprar crema para el cabello en Montevideo",
      "crema capilar precio Uruguay"
    ],
    "search_results_count": 45,
    "unique_urls_found": 23,
    "geographic_validated_urls": 21,
    "validation_retry_count": 0,
    "crawl_triggered": true,
    "product_page_candidates": [
      {
        "url": "https://farmacity.com/producto/crema-capilar",
        "title": "Crema Capilar Hidratante",
        "classification": "product_page",
        "confidence": 0.92,
        "validation_method": "domain_pattern"
      }
    ],
    "processing_time_ms": 2340,
    "validation_time_ms": 890
  }
  ```

## Usage Examples

### Basic Usage
```python
from src.core.agent import ProductSearchAgent

# Initialize agent for default market (Uruguay)
agent = ProductSearchAgent()

# Initialize agent for specific country/city
agent = ProductSearchAgent(country="AR", city="Buenos Aires")

# Execute search with validation
results = await agent.search_product("zapatos deportivos")
```

### Standalone URL Validation
```python
from src.core.geo_url_validator_agent import GeoUrlValidatorAgent

# For Uruguay
validator = GeoUrlValidatorAgent(country="UY", city="Montevideo")

# For Argentina
validator = GeoUrlValidatorAgent(country="AR", city="Buenos Aires")

# Validate URLs for specific market
urls = [
    "https://example.com.uy/products",
    "https://tiendainglesa.com.uy/calzado", 
    "https://mercadolibre.com.ar/item",
    "https://unrelated-site.com/content"
]

valid_urls = await validator.validate_urls(urls, "zapatos deportivos")
```

### API Usage with Geographic Parameters
```bash
# Default market (Uruguay)
curl "http://localhost:8000/search?product=laptop"

# Specific country
curl "http://localhost:8000/search?product=laptop&country=AR"

# Country with city
curl "http://localhost:8000/search?product=laptop&country=BR&city=São Paulo"
```

## Development Setup

### Prerequisites
- Python 3.8+
- Access to home server infrastructure (Ollama, Web Crawler)
- Tailscale VPN connection

### Installation
```bash
# Navigate to project directory
cd agents/product_search_agent

# Run setup script
./start.sh
```

### Environment Configuration
Create `.env` file (see `env.example`) with:
```bash
LOG_LEVEL=INFO
HOST=0.0.0.0
PORT=8000
OLLAMA_BASE_URL=http://home.server:30080/ollama
WEB_CRAWLER_BASE_URL=http://home.server:30080/crawler
BRAVE_SEARCH_API_KEY=your_api_key_here

# Relevance scoring and candidate gating
MAX_PRODUCT_CANDIDATES_FOR_PRICE=10
MIN_RELEVANCE_SCORE=0.2

# Vision fallback when text content is missing/insufficient
PRICE_VISION_ON_NO_TEXT=true

# Caching toggles + TTLs (seconds)
GEO_URL_CACHE_ENABLED=true
GEO_URL_CACHE_TTL_SECONDS=604800
PAGE_TYPE_CACHE_ENABLED=true
PAGE_TYPE_CACHE_TTL_SECONDS=21600
RELEVANCE_CACHE_ENABLED=true
RELEVANCE_CACHE_TTL_SECONDS=21600
PRICE_CACHE_ENABLED=true
PRICE_CACHE_TTL_SECONDS=7200

# Similarity-based product dedupe
PRODUCT_NAME_SIMILARITY_THRESHOLD=0.85

# Logging (secrets redacted unless explicitly enabled)
LOG_SENSITIVE_CONFIG=false
```

## Dependencies
- **FastAPI**: Web framework for API development
- **Shared Library**: Internal utilities for logging, database access, and service clients
- **Ollama Integration**: LLM services (llama3.2, deepseek-r1:1.5b)
- **Web Crawler Service**: Deep content extraction
- **Brave Search API**: External search functionality

## Architecture
- **Agent-based design**: Modular sub-agents for specific tasks
- **Async processing**: Full async/await implementation
- **Shared utilities**: Logging, database access, service clients
- **Error resilience**: Comprehensive error handling and fallbacks
- **Performance optimized**: Parallel processing and efficient batching

For detailed technical specifications, see [`prd/README.md`](prd/README.md).

## Related services (repo layout)
- **Web crawler**: `services/web_crawler/`
- **Renderer**: `services/renderer/`

## Current LLM Model Configuration

- **Query Generation**: `qwen3:latest` (temperature 0.0, JSON format)
- **Query Validation**: `qwen2.5:7b` (temperature 0.0, JSON format)
- **Product Page Classification**: `qwen3:latest` (temperature 0.1, JSON format)
- **Geographic URL Validation**: `qwen3:latest` primary, `phi3:latest` fallback (temperature 0.0/0.5, JSON format)
- **Price Extraction**: `qwen2.5:7b` (temperature 0.0, JSON format)

All LLM calls use strict JSON output via `format="json"` for reliable structured responses.

## Dependencies
- **Ollama Integration**: LLM services (qwen3:latest, qwen2.5:7b, phi3:latest)