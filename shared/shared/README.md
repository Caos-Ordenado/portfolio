# Agent Utilities

Shared utilities for AI agents running on the home server infrastructure. This package provides standardized components for database access, logging, caching, and interaction with core services.

## Quick Start

```bash
# Install in your agent's environment
pip install -e /path/to/shared/shared
```

```python
# Basic usage example
from shared import (
    setup_logger,
    DatabaseContext,
    RedisClient,
    WebCrawlerClient,
    LLMClient,
    MODEL_EXTRACT,
    RendererClient,
)

# Set up logging
logger = setup_logger("my_agent")

# Use the database
async with DatabaseContext() as db:
    # Database operations here
    pass

# Use Redis caching
async with RedisClient() as redis:
    # Redis operations here
    pass

# Use the web crawler
async with WebCrawlerClient() as crawler:
    # Crawling operations here
    pass

# Use the local LLM gateway (llama-swap, OpenAI-compatible)
async with LLMClient() as llm:
    # Structured JSON response
    response = await llm.generate(
        prompt="Return ONLY JSON per schema...",
        model=MODEL_EXTRACT,
        temperature=0.0,
        num_predict=512,
        format="json",
    )

# Use the Renderer service (Playwright over HTTP)
async with RendererClient(base_url="http://renderer.default.svc.cluster.local:8000") as renderer:
    # Playwright renderer operations here
    pass
```

## Core Components

### 1. Database System
Provides a unified database interface with PostgreSQL storage and Redis caching.

```python
from shared.database import DatabaseContext
from shared.models import WebPage

async with DatabaseContext() as db:
    # Create and save a webpage
    webpage = WebPage(
        url="https://example.com",
        title="Example Domain",
        text="Example content",
        links=["https://www.example.com/page1"],
        metadata={
            "language": "en",
            "content_type": "text/html"
        }
    )
    await db.webpages.save(webpage)
    
    # Query webpages
    recent_pages = await db.webpages.get_recent_pages(limit=5)
```

#### WebPage Model
The `WebPage` model is optimized for RAG (Retrieval-Augmented Generation) applications:

```python
from shared.models import WebPage

webpage = WebPage(
    url="https://example.com",
    title="Title",
    description="Meta description",
    main_content="Main article content",
    full_text="Complete page text",
    headers={
        "h1": ["Main Header"],
        "h2": ["Subheader 1", "Subheader 2"]
    },
    meta_tags={
        "description": "Page description",
        "keywords": "key, words"
    },
    structured_data=[],  # JSON-LD data
    links=["https://example.com/page1"],
    images=[{
        "src": "image.jpg",
        "alt": "Description"
    }],
    content_language="en"
)
```

### 2. Web Crawler Client
Interface to the web crawler service for content retrieval:

```python
from shared.web_crawler_client import WebCrawlerClient, CrawlRequest

async with WebCrawlerClient() as crawler:
    # Check service health
    is_healthy = await crawler.health_check()
    
    # Configure crawl request
    request = CrawlRequest(
        urls=["https://example.com"],
        max_pages=10,
        max_depth=2,
        allowed_domains=["example.com"],
        exclude_patterns=["/admin/", "/login/"],
        respect_robots=True
    )
    
    # Execute crawl
    response = await crawler.crawl(request)
    
    # Process results
    for result in response.results:
        print(f"URL: {result.url}")
        print(f"Title: {result.title}")
```

### 3. LLM Client
Interface to the local LLM gateway (llama-swap + llama.cpp on the home server, OpenAI-compatible `/v1/chat/completions`). See `portfolio/k8s/llama/README.md` for the runbook.

- Env vars: `LLM_BASE_URL` (default `http://home.server:30080/llm`), `LLM_MODEL` (default `extract`), `LLM_TIMEOUT_S` (default `180`).
- Model alias constants: `MODEL_CODER` (`coder`), `MODEL_REASONING` (`reasoning`), `MODEL_EXTRACT` (`extract`), `MODEL_VISION` (`vision`).
- Only one model is loaded at a time; llama-swap swaps models on demand and queues requests.
- Failures raise `LLMError`.

```python
from shared.llm_client import LLMClient, LLMError, MODEL_EXTRACT, MODEL_REASONING

async with LLMClient() as llm:
    # Simple generation
    response = await llm.generate(
        "Summarize this text: ...",
        model=MODEL_EXTRACT
    )
    
    # Advanced options
    response = await llm.generate(
        prompt="Your prompt here",
        model=MODEL_REASONING,
        temperature=0.7,
        num_predict=500,
        format=None,
    )
```

### 4. Renderer Client (Playwright-as-a-Service)
- Use `RendererClient` to call the dedicated renderer service for screenshots or HTML rendering.
- Prefer this over bundling Playwright in agents.

```python
from shared.renderer_client import RendererClient

async with RendererClient(base_url=os.getenv("RENDERER_URL", "http://renderer.default.svc.cluster.local:8000")) as renderer:
    data = await renderer.render_html(
        url="https://example.com",
        wait_for_selector="body",
        timeout_ms=30000,
        viewport_randomize=True,
    )
    # data: { "url": str, "html": str, "text": str }
```

Env vars:
- `RENDERER_URL` (default depends on deployment; in-cluster: `http://renderer.default.svc.cluster.local:8000`)

### 5. Redis Client
Thread-safe Redis client with connection pooling:

```python
from shared.redis_client import RedisClient

async with RedisClient() as redis:
    # Basic operations
    await redis.set("key", "value", ex=3600)  # 1 hour expiration
    value = await redis.get("key")
    
    # List operations
    await redis.lpush("list_key", "item1", "item2")
    items = await redis.lrange("list_key", 0, -1)
    
    # Hash operations
    await redis.hset("hash_key", mapping={"field1": "value1"})
    value = await redis.hget("hash_key", "field1")
```

### 6. Logging System
Standardized logging configuration:

```python
from shared.logging import setup_logger

# Create logger
logger = setup_logger("my_agent")

# Usage
logger.debug("Debug message")
logger.info("Info message")
logger.warning("Warning message")
logger.error("Error message")
```

### 7. Utility Functions
Common utility functions for LLM response parsing and URL handling.

#### JSON Utilities
Handle common issues with LLM-generated JSON responses:

```python
from shared.utils import strip_json_code_block, fix_truncated_json, remove_json_comments

# Clean LLM response that may have markdown fences
raw_response = '```json\n{"key": "value"}\n```'
clean_json = strip_json_code_block(raw_response)  # '{"key": "value"}'

# Fix truncated JSON from token limits
partial = '{"items": ["a", "b"'
fixed = fix_truncated_json(partial)  # '{"items": ["a", "b"]}'

# Remove comments that LLMs sometimes add
commented = '{"key": "value" // this is a comment\n}'
clean = remove_json_comments(commented)
```

#### URL Utilities
Generic URL handling for deduplication and domain comparison:

```python
from shared.utils import same_domain, normalize_url, dedupe_urls_preserve_order

# Check domain equality
same_domain("https://example.com/page1", "https://example.com/page2")  # True

# Normalize for deduplication (removes www, tracking params, trailing slashes)
normalize_url("https://www.example.com/page?utm_source=foo")  # "https://example.com/page"

# Deduplicate preserving order
dedupe_urls_preserve_order(["a", "b", "a", "c"])  # ["a", "b", "c"]
```

#### Fallback Extraction
Last-resort extraction when JSON parsing fails:

```python
from shared.utils import extract_fields_from_partial_json

# Extract fields from malformed/truncated JSON using regex
partial = '{"page_type": "PRODUCT", "name": "Test'
fields = extract_fields_from_partial_json(partial, ['page_type', 'name'])
# {'page_type': 'PRODUCT'}
```

## Configuration

### Environment Variables

```bash
# Database Configuration
POSTGRES_HOST=home.server
POSTGRES_PORT=5432
POSTGRES_DB=web_crawler
POSTGRES_USER=admin
POSTGRES_PASSWORD=<get_password_from_secret_manager>

# Redis Configuration
REDIS_HOST=home.server
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=<get_password_from_secret_manager>

# Logging Configuration
LOG_LEVEL=DEBUG  # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Renderer Service
RENDERER_URL=http://renderer.default.svc.cluster.local:8000
```

## Best Practices

1. **Resource Management**
   ```python
   # ALWAYS use async context managers
   async with DatabaseContext() as db:
       async with RedisClient() as redis:
           # Your code here
           pass
   ```

2. **Error Handling**
   ```python
   try:
       async with WebCrawlerClient() as crawler:
           response = await crawler.crawl(urls=["https://example.com"])
   except ConnectionError:
       logger.error("Failed to connect to crawler service")
   except Exception as e:
       logger.exception("Unexpected error during crawl")
   ```

3. **Logging**
   ```python
   # Use appropriate log levels
   logger.debug("Detailed information for debugging")
   logger.info("General information about program execution")
   logger.warning("Warning messages for potentially harmful situations")
   logger.error("Error messages for serious problems")
   ```

4. **Database Operations**
   ```python
   # Use repositories for data access
   async with DatabaseContext() as db:
       # Fetch data
       pages = await db.webpages.get_by_domain("example.com")
       
       # Batch operations
       await db.webpages.save_many(pages)
   ```

## Development Notes

- All clients are async/await compatible
- Thread-safe logging and connection pooling
- Automatic resource cleanup with context managers
- Type hints and validation throughout
- Error handling patterns established

## Support

For issues or questions:
1. Check the docstrings in the code
2. Review the examples in this README
3. Check the logs in `server.log`
