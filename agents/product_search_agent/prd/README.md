# Product Search Agent - Product Requirements Document (PRD)

## 1. Executive Summary

The **Product Search Agent** is an intelligent web service that automates the discovery and classification of e-commerce product pages based on natural language product queries. It leverages AI-powered query generation, web search, intelligent crawling, and machine learning classification to provide highly relevant product results for any specified geographic market.

### Key Value Propositions
- **AI-Enhanced Query Generation**: Converts simple product names into optimized search queries with purchase intent
- **Intelligent Product Page Discovery**: Automatically identifies and classifies potential product pages
- **Proactive Web Crawling**: Triggers comprehensive content extraction for deeper product analysis
- **Geographic Targeting**: Configurable for any country or city-specific e-commerce landscape with intelligent URL validation

## 2. Product Overview

### 2.1 Core Functionality
The agent serves as a unified API endpoint that orchestrates multiple AI-powered sub-agents to transform basic product queries into comprehensive product discovery results.

### 2.2 Target Users
- E-commerce aggregators
- Price comparison platforms
- Market research analysts
- Automated shopping assistants
- Product availability monitoring systems

### 2.3 Success Metrics
- Query generation accuracy and relevance
- Product page classification precision
- API response time and reliability
- Web crawling efficiency and coverage

## 3. Technical Architecture

### 3.1 System Architecture
```
┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
│   FastAPI       │    │  ProductSearchAgent  │    │  External APIs  │
│   Web Server    │───▶│      (Core)          │───▶│  (Brave Search) │
│   Port: 8000    │    │                      │    │                 │
└─────────────────┘    └──────────────────────┘    └─────────────────┘
                                   │
                   ┌───────────────┼───────────────┐
                   ▼               ▼               ▼
          ┌─────────────┐ ┌─────────────┐ ┌──────────────┐
          │ Query Gen   │ │ Search      │ │ URL Extract  │
          │ Agent       │ │ Agent       │ │ Agent        │
          └─────────────┘ └─────────────┘ └──────────────┘
                   │               │               │
                   ▼               ▼               ▼
          ┌─────────────┐ ┌─────────────┐ ┌──────────────┐
          │ LLM Gateway │ │ Brave       │ │ Web Crawler  │
          │ (llama-swap)│ │ Search      │ │ Service      │
          └─────────────┘ └─────────────┘ └──────────────┘
                                   │
                            ┌──────┴──────┐
                            ▼             ▼
                   ┌─────────────┐ ┌─────────────┐
                   │ Geographic  │ │ Product Page│
                   │ URL         │ │ Classifier  │
                   │ Validator   │ │ (Phase 5)   │
                   │ (Phase 2.5) │ │             │
                   └─────────────┘ └─────────────┘
                            │             │
                            ▼             ▼
                   ┌──────────────┐ ┌───────────────┐
                   │ extract      │ │ extract       │
                   │ (Qwen3.5-9B) │ │ (Qwen3.5-9B)  │
                   └──────────────┘ └───────────────┘
```

### 3.2 Infrastructure Dependencies

#### Core Infrastructure (Home Server - MicroK8s Cluster)
- **Host**: home.server (internal VPN address)
- **Access**: Tailscale VPN for remote connectivity
- **Ingress**: Traefik reverse proxy

#### Required Services
- **LLM Gateway (llama-swap + llama.cpp)**: 
  - URL: `http://home.server:30080/llm` (OpenAI-compatible; runbook `portfolio/k8s/llama/README.md`)
  - Models: `extract` alias (Qwen3.5-9B, primary for all text steps), `vision` alias (Gemma 4 26B-A4B, screenshot price fallback)
  - Purpose: AI query generation, validation, URL validation, and product page classification

- **Web Crawler Service**:
  - URL: `http://home.server:30080/crawler/`
  - Purpose: Deep content extraction and page analysis
  - Namespace: default

- **PostgreSQL Database**:
  - Service: `postgres.shared.svc.cluster.local:5432`
  - Database: `web_crawler`
  - Purpose: Persistent storage for crawled content

- **Redis Cache**:
  - Service: `redis.shared.svc.cluster.local:6379`
  - Purpose: Performance caching layer

### 3.3 Shared Libraries
The agent utilizes the `shared` package for common functionality:

- **Logging System**: Thread-safe logging with loguru
- **Database Access**: Repository pattern with PostgreSQL + Redis caching
- **Service Clients**: Async HTTP clients for the LLM gateway and Web Crawler
- **Configuration Management**: Environment-based configuration

## 4. Functional Requirements

### 4.1 Primary Workflow

#### Phase 1: Query Generation & Validation
**Input**: Natural language product name (e.g., "crema para el cabello")
**Process**: 
- **Step 1.1**: Uses the LLM gateway (`extract` model) to generate 5 optimized search queries
- **Step 1.2**: Validates queries using the `extract` model to ensure relevance and purchase intent
- Queries designed with purchase intent and geographic context
- JSON-formatted output with specific search terms

**Output**: List of validated search queries (example for Uruguay market)
```json
[
  "comprar crema para el cabello en Montevideo",
  "crema capilar precio Uruguay",
  "productos para el cabello tienda online Uruguay",
  "crema hidratante cabello venta Montevideo",
  "tratamiento capilar comprar online"
]
```

**Note**: Query generation adapts to the specified country and city parameters, automatically incorporating relevant geographic terms, local language variations, and market-specific e-commerce platforms.

#### Phase 2: Web Search Execution
**Input**: Validated search queries
**Process**:
- Executes parallel searches using Brave Search API
- Aggregates results from all queries
- Collects URLs, titles, descriptions, and snippets

**Output**: Comprehensive search results dataset

#### Phase 2.5: Geographic URL Validation
**Input**: Raw search results from Phase 2
**Process**: 
- Country-specific domain pattern matching (e.g., .uy, .com.uy for Uruguay; .ar, .com.ar for Argentina)
- LLM-based contextual analysis using the `extract` model with geographic prompts
- Geographic indicator detection in URLs and metadata for the specified country/city
- Business name pattern recognition for local retailers and e-commerce platforms
- Multi-country support: UY, AR, BR, CL, CO, PE, EC, MX, US, ES
- Retry logic: Up to 3 iterations with query refinement when < 20 valid URLs found

**Output**: Filtered list of geographically-relevant URLs (target: 20+ URLs)
**Input Parameters**: `country` (required, default: "UY"), `city` (optional)
**Performance**: < 2 seconds per batch processing

#### Phase 3: URL Extraction and Pre-filtering
**Input**: Geographic-validated search results
**Process**:
- **Stage 1**: Pattern-based filtering using 22+ exclusion patterns (navigation, auth, files)
- **Stage 2**: Advanced duplicate detection with URL normalization and domain rate limiting
- **Stage 3**: LLM bulk classification for large URL sets (threshold: 20+ URLs)
- Extracts unique URLs and removes invalid content types
- Domain rate limiting (max 8 URLs per domain) to prevent over-representation

**Output**: Pre-filtered, high-quality candidate URLs (50-90% reduction from input)

#### Phase 4: Proactive Web Crawling
**Input**: Extracted URLs
**Process**:
- Triggers asynchronous crawling via Web Crawler Service
- Configurable parameters:
  - max_pages: 10 per URL
  - max_depth: 2 levels
  - timeout: 180 seconds
  - concurrent_pages: 5

**Output**: Crawl job initiation confirmation

#### Phase 5: Product Page Classification
**Input**: Candidate URLs
**Process**:
- Uses the LLM gateway (`extract`) to classify URLs as product or category pages
- Analyzes URL patterns, domain context, and available metadata
- Multi-class classification (PRODUCT, CATEGORY) with confidence scoring

**Output**: Classified product page candidates
```json
{
  "product_pages": [
    {
      "url": "https://example.com/product/hair-cream",
      "classification": "product_page",
      "confidence": 0.95,
      "reasoning": "URL contains product identifier and domain is e-commerce"
    }
  ]
}
```

#### Phase 6: Category Expansion (Optional)
**Input**: URLs classified as CATEGORY pages
**Process**:
- Crawls category pages to extract individual product URLs
- Validates extracted URLs against geographic criteria
- Preserves existing PRODUCT page candidates

**Output**: Expanded list of product page candidates

#### Phase 7: Price Extraction
**Input**: Identified product page candidates
**Process**:
- Uses the LLM gateway (`extract`, with `vision` screenshot fallback) to extract product prices and details
- Analyzes page content, titles, and structured data
- Returns products sorted by price

**Output**: Products with extracted price information
```json
{
  "products_with_prices": [
    {
      "url": "https://example.com/product/hair-cream",
      "title": "Premium Hair Cream",
      "price": 25.99,
      "currency": "USD",
      "availability": "in_stock"
    }
  ]
}
```

### 4.2 API Specification

#### Endpoint: GET /search
**Description**: Primary search endpoint for product discovery

**Parameters**:
- `product` (string, required): Product name or description to search for
- `country` (string, optional): Country code for geographic filtering (default: "UY")
  - Supported: UY, AR, BR, CL, CO, PE, EC, MX, US, ES
- `city` (string, optional): City name for more specific geographic validation

**Response Format**:
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

**Error Responses**:
- `400 Bad Request`: Missing or invalid product parameter
- `500 Internal Server Error`: Service failures or LLM errors
- `503 Service Unavailable`: Dependent services unreachable

## 5. Technical Implementation

### 5.1 Agent Architecture

#### ProductSearchAgent (Core Orchestrator)
- **File**: `src/core/agent.py`
- **Purpose**: Coordinates all sub-agents and manages the complete workflow
- **Key Methods**:
  - `search_product()`: Main entry point for product search

#### Sub-Agents

1. **QueryGeneratorAgent**
   - **File**: `src/core/query_generator.py`
   - **Purpose**: AI-powered search query generation
   - **LLM Integration**: LLM gateway with `extract` model (temperature 0.0, JSON format)

2. **QueryValidatorAgent**
   - **File**: `src/core/query_validator.py`
   - **Purpose**: Validates and filters generated search queries
   - **LLM Integration**: LLM gateway with `extract` model (temperature 0.0, JSON format)

3. **SearchAgent**
   - **File**: `src/core/search_agent.py`
   - **Purpose**: External search API integration (Brave Search)
   - **Features**: Result aggregation and deduplication

4. **UrlExtractorAgent**
   - **File**: `src/core/url_extractor_agent.py`
   - **Purpose**: URL extraction with intelligent pre-filtering and web crawling orchestration
   - **Features**: 3-stage filtering pipeline (pattern-based → duplicate detection → LLM bulk classification)
   - **Performance**: 50-90% URL reduction before downstream processing
   - **Integration**: Web Crawler Service trigger

5. **GeoUrlValidatorAgent**
   - **File**: `src/core/geo_url_validator_agent.py`
   - **Purpose**: Parametrized geographic validation of URLs for any country/city
   - **LLM Integration**: LLM gateway with `extract` model (pattern-based fallback when the LLM is unavailable)
   - **Features**: Multi-country domain patterns, contextual analysis, retry logic
   - **Countries Supported**: UY, AR, BR, CL, CO, PE, EC, MX, US, ES

6. **ProductPageCandidateIdentifierAgent**
   - **File**: `src/core/product_page_candidate_identifier.py`
   - **Purpose**: AI-powered product page classification
   - **LLM Integration**: LLM gateway with `extract` model (temperature 0.1, JSON format)

7. **PriceExtractorAgent**
   - **File**: `src/core/price_extractor.py`
   - **Purpose**: Extracts product prices from identified product pages
   - **LLM Integration**: LLM gateway with `extract` model (temperature 0.0, JSON format)

8. **CategoryExpansionAgent**
   - **File**: `src/core/category_expansion_agent.py`
   - **Purpose**: Expands category pages into individual product page candidates
   - **Integration**: Web Crawler Service for page analysis

### 5.2 Geographic URL Validator Agent

#### Overview
The GeoUrlValidatorAgent is responsible for filtering search results to ensure they are relevant to the specified geographic location (country and optionally city). This component sits between Phase 2 (Web Search) and Phase 2.5 (URL Validation) in the workflow, providing configurable geographic localization for better product discovery across multiple markets.

#### Model Selection
- **Primary model**: `extract` (Qwen3.5-9B via llama-swap)
- **Selection rationale**: Balance of accuracy and performance for classification tasks
- **Optimized for**: Geographic context analysis and URL classification
- **Performance**: Low latency (< 500ms per inference)
- **Fallback**: Pattern-based validation when LLM unavailable

#### Validation Criteria

**1. Domain-based validation** (country-specific):
- Country TLDs (e.g., .uy, .com.uy for Uruguay; .ar, .com.ar for Argentina)
- Known country-specific e-commerce platforms (mercadolibre variants, local retailers)
- Geographic subdomains (country.site.com, countrycode.site.com)

**2. Content-based validation** (parametrized):
- URL path analysis (/country/, /country-code/, /major-cities/)
- Query parameter detection (country=code, region=country-name)
- Business name pattern recognition for local retailers per country
- City-specific validation when city parameter provided
- Contextual relevance analysis using LLM with geographic prompts

**3. Multi-country support**:
- **Supported countries**: UY, AR, BR, CL, CO, PE, EC, MX, US, ES
- **Language support**: Spanish, Portuguese, English (automatic detection)
- **Regional platforms**: Amazon, MercadoLibre variants, local e-commerce sites

#### Integration Points
- **Input**: Receives raw search results from Web Search Agent (Phase 2)
- **Output**: Provides filtered URLs to URL Extraction Agent (Phase 3)
- **Configuration**: Configurable threshold for minimum valid URLs (default: 20)
- **Performance**: Processes batches within 2 seconds

#### Retry Logic
- **Trigger condition**: When fewer than target number (20) of valid URLs found
- **Maximum iterations**: 3 retry cycles to prevent infinite loops
- **Query refinement**: Progressive enhancement with stronger geographic context for specified country
- **Fallback strategy**: Heuristic-only validation if LLM services unavailable
- **Query enhancement**: Uses LLM to add country and city-specific terms with local language support
- **Adaptive**: Automatically adjusts enhancement strategy based on country/language (e.g., Spanish terms for LATAM countries)

#### Error Handling and Resilience
- Structured logging for validation decisions and performance metrics
- Graceful degradation with fallback to pattern-based validation
- Automatic retry with exponential backoff for transient errors
- Comprehensive error logging for debugging and monitoring
- Metrics tracking for validation success rates

### 5.3 Service Integration

#### Web Crawler Integration
- **Client**: `WebCrawlerClient` from shared library
- **Trigger Service**: `WebCrawlerTriggerService`
- **Data Retrieval**: `WebCrawlerDataRetrievalService`
- **Purpose**: Deep content extraction and persistent storage

#### LLM Integration
- **Client**: `LLMClient` from shared library (`shared.llm_client`)
- **Models in Use**:
  - `extract` (query generation, query validation, product page classification, geographic URL validation, price extraction)
  - `vision` (screenshot-based price extraction fallback)
- **Parameters**: 
  - `temperature`: 0.0 (for deterministic JSON outputs in validation/extraction)
  - `temperature`: 0.1 (for slight creativity in generation tasks)
  - `temperature`: 0.5 (for query enhancement creativity)
  - `format`: "json" (for all structured outputs)
- **Usage**: Query generation, validation, URL geographic validation, product page classification, and price extraction

### 5.3 Data Models

#### API Models
```python
class SearchRequest(BaseModel):
    product: str
    country: str = "UY"
    city: Optional[str] = None

class ProductPageCandidate(BaseModel):
    url: str
    title: Optional[str] = None
    classification: str
    confidence: float
    reasoning: Optional[str] = None

class SearchResponse(BaseModel):
    success: bool
    query: str
    generated_queries: List[str]
    search_results_count: int
    unique_urls_found: int
    crawl_triggered: bool
    product_page_candidates: List[ProductPageCandidate]
    processing_time_ms: int
```

## 6. Deployment and Operations

### 6.1 Local Development Setup

#### Prerequisites
- Python 3.8+
- Access to home server infrastructure
- Tailscale VPN connection

#### Installation Steps
```bash
# Navigate to project directory
cd agents/product_search_agent

# Run setup script
./start.sh
```

#### Environment Configuration
Create `.env` file with:
```bash
LOG_LEVEL=INFO
HOST=0.0.0.0
PORT=8000
LLM_BASE_URL=http://home.server:30080/llm
WEB_CRAWLER_BASE_URL=http://home.server:30080/crawler
BRAVE_SEARCH_API_KEY=your_api_key_here
```

### 6.2 Dependencies

#### Core Dependencies
- **FastAPI**: Web framework for API development
- **Uvicorn**: ASGI server for production deployment
- **Langchain**: AI/LLM integration framework
- **Pydantic**: Data validation and serialization
- **Shared Library**: Internal utilities and service clients

#### Shared Library Components
- **Logging**: `shared.logging` - Thread-safe logging with loguru
- **Database**: `shared.repositories` - Repository pattern with caching
- **Service Clients**: 
  - `shared.llm_client` - LLM gateway integration
  - `shared.web_crawler_client` - Web crawling service integration

### 6.3 Monitoring and Logging

#### Log Management
- **File**: `server.log` (automatically created)
- **Rotation**: 100MB file size limit
- **Retention**: 5 days
- **Format**: Structured JSON with timestamps
- **Levels**: DEBUG, INFO, WARNING, ERROR, CRITICAL

#### Health Monitoring
- **Endpoint**: `/health` (if implemented)
- **Dependencies Check**: LLM gateway and Web Crawler service availability
- **Metrics**: Response times, error rates, throughput

### 6.4 Performance Considerations

#### URL Pre-filtering Performance
- **3-Stage Pipeline**: Optimized filtering reduces processing overhead by 50-90%
- **Pattern-based Stage**: Fast regex filtering excludes obvious non-product URLs
- **Domain Rate Limiting**: Prevents over-representation with configurable thresholds
- **LLM Bulk Processing**: Efficient batch classification for large URL sets (20+ threshold)
- **Graceful Fallbacks**: System continues if any stage fails

#### URL Validation Performance
- **Batch Processing**: Validates URLs in efficient batches to minimize latency
- **Caching Mechanism**: Previously validated URLs cached for 24 hours
- **Parallel Processing**: Domain validation performed in parallel threads
- **Adaptive Batching**: LLM analysis uses optimal batch sizes (5-10 URLs per request)
- **Graceful Degradation**: Falls back to pattern-only validation if LLM unavailable
- **Performance Target**: Complete validation within 2 seconds per batch

#### Retry Logic Optimization
- **Smart Thresholds**: Only triggers retry when < 20 validated URLs found
- **Progressive Enhancement**: Each retry iteration adds stronger geographic context for the specified country
- **Maximum Bounds**: Limits to 3 retry iterations to prevent delays
- **Query Refinement**: Uses LLM to generate enhanced queries with local terms
- **Fallback Strategy**: Pattern-based enhancement when LLM services fail

### 6.5 Scaling Considerations

#### Performance Optimization
- **Async Processing**: All I/O operations are asynchronous
- **Connection Pooling**: Reuse HTTP connections to external services
- **Caching**: Redis integration for frequently accessed data
- **Parallel Execution**: Concurrent search query processing

#### Resource Requirements
- **Memory**: ~512MB base + LLM processing overhead
- **CPU**: Multi-core recommended for concurrent processing
- **Network**: Stable connection to home server infrastructure
- **Storage**: Minimal local storage (logs only)

## 7. Future Enhancements

### 7.1 Planned Features
- **Price Extraction**: Integration with price extraction agent
- **Product Database**: Persistent storage for discovered products
- **Real-time Updates**: WebSocket support for live search results
- **Analytics Dashboard**: Search patterns and performance metrics

### 7.2 Integration Opportunities
- **E-commerce APIs**: Direct integration with major retailers per supported country
- **Notification System**: Alerts for new product discoveries
- **Machine Learning**: Enhanced classification with custom models
- **Multi-language Support**: Portuguese and English market expansion

## 8. Risk Assessment

### 8.1 Technical Risks
- **LLM Service Availability**: Dependency on LLM gateway (llama-swap) uptime
- **Rate Limiting**: External search API quotas and throttling
- **Infrastructure Dependency**: Single point of failure with home server

### 8.2 Mitigation Strategies
- **Service Health Checks**: Automated monitoring and alerting
- **Graceful Degradation**: Fallback mechanisms for service failures
- **Error Handling**: Comprehensive exception management
- **Retry Logic**: Exponential backoff for transient failures

## 9. Success Criteria

### 9.1 Performance Metrics
- **Response Time**: < 5 seconds for typical product searches
- **URL Validation**: < 2 seconds for validation phase completion
- **Accuracy**: > 95% precision in geographic URL identification across supported countries
- **Product Classification**: > 85% precision in product page classification
- **Validation Success**: Achieve 20+ validated URLs in > 90% of searches
- **Availability**: > 99% uptime for API endpoint
- **Throughput**: Support for concurrent search requests

### 9.2 Quality Metrics
- **Relevance**: Search results match user intent
- **Coverage**: Comprehensive discovery across e-commerce platforms in supported countries
- **Freshness**: Recent product listings prioritized
- **Diversity**: Results span multiple retailers and categories

---

**Document Version**: 1.0  
**Last Updated**: 2024  
**Maintained By**: Product Search Agent Development Team 