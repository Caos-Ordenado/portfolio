# Web Crawler Service

A FastAPI service for crawling web pages, optionally persisting results via the shared storage layer (PostgreSQL + Redis),
and providing a vision extraction endpoint via the shared Renderer + local LLM gateway (llama-swap).

## Features

- Asynchronous web crawling with aiohttp
- Best-effort **memory backoff** (prevents exhaustion by pausing when the process memory exceeds `CRAWLER_MEMORY_THRESHOLD`)
- Optional **robots.txt** enforcement when `respect_robots=true` in the request (best-effort; failures to fetch/parse robots allow crawling)
- URL filtering:
  - `allowed_domains` (exact netloc match)
  - `exclude_patterns` (glob patterns like `*.pdf` using `fnmatch` against the full URL; patterns without glob metacharacters fall back to substring matching)
- Optional persistence:
  - When DB is available, crawled pages are persisted via the shared `DatabaseContext`
  - When DB is not available, crawling still works but results are not persisted
- RESTful API with FastAPI
  - OpenAPI/Swagger documentation
  - Request/response validation
  - Async request handling
  - Health check endpoint
  - CORS support
- Vision extraction endpoint (`/extract-vision`) using:
  - shared Renderer service (Playwright-as-a-Service)
  - shared LLM client (`vision` model alias, JSON-only response)

## To run locally

1. Create a virtual environment (recommended):
```bash
python -m venv .venv
source .venv/bin/activate  # On Unix/macOS
# or
.venv\Scripts\activate  # On Windows
```

2. Set up environment variables:
```bash
cp .env.example .env
```
Then edit `.env` with your specific settings.

3. Start the server:

```bash
./start.sh
```

### Automated Deployment
Use the deployment script for automated updates:
```bash
cd services/web_crawler
./deploy.sh
```

## Project Structure

```
web_crawler/
├── src/
│   ├── core/
│   │   ├── __init__.py      # Core package exports
│   │   ├── crawler.py       # Web crawler implementation
│   │   ├── models.py        # Shared data models
│   ├── api/
│   │   ├── __init__.py      # API package exports
│   │   └── app.py          # FastAPI application
│   ├── main.py             # Entry point (FastAPI server or example)
│   └── config.py           # Configuration management
├── pyproject.toml          # Project dependencies
└── start.sh                # Local dev startup (installs shared + this package, then runs uvicorn)
```

## Environment Variables

### Service runtime settings (used by the FastAPI server)
```env
LOG_LEVEL=INFO

CRAWLER_MEMORY_THRESHOLD=80.0
CRAWLER_USER_AGENT=custom_agent
CRAWLER_DEBUG=false

CRAWLER_CLEANUP_INTERVAL_HOURS=24
CRAWLER_DATA_RETENTION_DAYS=30

# robots.txt enforcement cache (used only when respect_robots=true in requests)
CRAWLER_ROBOTS_CACHE_TTL_SECONDS=3600

# vision extraction
CRAWLER_VIEWPORT_HEIGHT=1080
CRAWLER_VIEWPORT_WIDTH=1920
RENDERER_URL=http://home.server:30080/renderer
LLM_BASE_URL=http://home.server:30080/llm
LLM_MODEL=vision
```

### Per-request crawl controls (these are NOT read from env by the server)
These are controlled via the HTTP request body (`shared.interfaces.web_crawler.CrawlRequest`), which already defines defaults:

- `max_pages` (default `10000`)
- `max_depth` (default `20`)
- `timeout` (ms, default `180000`)
- `max_total_time` (s, default `300`)
- `max_concurrent_pages` (default `10`)
- `allowed_domains` (optional)
- `exclude_patterns` (optional)
- `respect_robots` (default `false`)

If you keep `CRAWLER_MAX_PAGES`, `CRAWLER_TIMEOUT`, etc. in your local `.env`, it’s harmless — it just won’t affect the FastAPI server unless you use the **example runner** (`python -m src.main example`) or `src/config.py` in your own scripts.

### Storage Settings
```env
POSTGRES_HOST=postgres.shared.svc.cluster.local
POSTGRES_PORT=5432
POSTGRES_USER=admin
POSTGRES_DB=web_crawler
POSTGRES_PASSWORD=...
REDIS_HOST=redis.shared.svc.cluster.local
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=...
```

### Running the FastAPI Server

The project includes a `start.sh` script that handles environment setup and server startup:

```bash
# Start the server using the script
./start.sh
```

The script will:
1. Clean the server.log file
2. Load environment variables
3. Install the shared package and this service in editable mode
4. Start the FastAPI server

The FastAPI server will be available at:
- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- ReDoc UI: http://localhost:8000/redoc

### API Endpoints
The **authoritative request/response schemas** are the Pydantic models in `shared.interfaces.web_crawler`.

- `GET /health`
- `POST /crawl` (request: `CrawlRequest`, response: `CrawlResponse`)
- `POST /crawl-single` (request: `SingleCrawlRequest`, response: `SingleCrawlResponse`)
- `POST /extract-vision` (request: `VisionExtractRequest`, response: `VisionExtractResponse`)

#### Vision Extraction

Request:

```json
{
  "url": "https://example.com/product",
  "fields": ["name", "price", "currency", "availability"],
  "timeout": 60000
}
```

Response:

```json
{
  "success": true,
  "data": {"name": "...", "price": 123.45, "currency": "USD", "availability": "In stock"},
  "elapsed_time": 1.23
}
```

## Output and Logging

The crawler provides detailed output with configurable logging levels:

1. Console Output:
   - Shows real-time progress of the crawl
   - Displays basic information about crawled pages
   - Shows any errors or issues during crawling

2. Log File (`server.log`):
   - Contains detailed logging information based on LOG_LEVEL
   - Includes timestamps for each operation
   - Shows full crawl results and memory usage when debug is enabled
   - Log file is cleaned on each server start
   - Available log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

### Automated Deployment
The project includes a `deploy.sh` script that automates the entire process:

```bash
# From the web_crawler directory
./deploy.sh
```

The script will:
1. Create ConfigMap from your .env file (excluding sensitive data)
2. Build the Docker image for AMD64 architecture
3. Transfer the image to your home server
4. Import it into microk8s
5. Apply Kubernetes configurations
6. Force a rollout restart
7. Wait for deployment completion

After deployment, the web crawler will be accessible at:
- API Endpoint: `http://home.server/crawler/`
- Swagger UI: `http://home.server/crawler/docs`
- ReDoc UI: `http://home.server/crawler/redoc`

### Storage Configuration

The web crawler uses two storage backends:

#### PostgreSQL
Available in the shared namespace:
- Inside cluster: `postgres.shared.svc.cluster.local:5432`
- Database: `web_crawler`
- User: `admin`

When using PostgreSQL storage:
- Use the correct service DNS name
- Check database connectivity before crawling large sites

#### Redis
Available in the shared namespace:
- Inside cluster: `redis.shared.svc.cluster.local:6379`

When using Redis storage:
 - Use the correct service DNS name
 - Monitor Redis memory usage for large crawls

### Vision Extraction Endpoint

- Path: `/extract-vision`
- Purpose: Navigate with Playwright (Chromium), capture a full-page screenshot, and extract structured fields using the `vision` model (Gemma 4 26B-A4B via llama-swap).
- Uses non-sensitive env vars: `LLM_BASE_URL`, `LLM_MODEL`

Example curl
```bash
curl -s http://home.server:30080/crawler/extract-vision \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/product",
    "fields": ["name", "price", "currency", "availability"],
    "timeout": 60000
  }'
```