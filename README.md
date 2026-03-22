# DevShowcase-Platform

**Technical Portfolio Showcase** by Nelson Fabian Rey

## 🎯 Purpose

This repository demonstrates professional capabilities through a live, self-hosted infrastructure. **DevShowcase-Platform** serves as a technical portfolio showcasing expertise in:

- **Solutions Architecture**: Cloud-native system design and scalability
- **Full Stack Development**: End-to-end application development
- **Kubernetes Orchestration**: Production-grade container orchestration
- **Infrastructure-as-Code**: Automated deployment and configuration
- **AI Agent Development**: Custom intelligent automation systems
- **Observability**: Comprehensive monitoring and logging stacks

## 🚀 What's Running

### Live Infrastructure (24/7)
- **8+ Microservices** deployed on Kubernetes
- **AI agents + services** for web crawling, rendering, and product search
- **GPU-Accelerated LLM** inference with Ollama
- **Comprehensive Monitoring** via Prometheus, Grafana, and Loki
- **Production Security** with zero-trust architecture

### Technical Stack
- **Orchestration**: MicroK8s, Traefik Ingress, Kustomize
- **Backend**: Python (FastAPI, AsyncIO), Node.js
- **Data Layer**: PostgreSQL + pgvector, Redis
- **AI/ML**: Ollama LLM, Vision Models, Custom Agents
- **Observability**: Prometheus, Grafana, Loki, Promtail
- **Security**: TLS/SSL, Secret Management

## 🗺️ Home Server Routing & Endpoints (MicroK8s)

### Entry points (how you reach the cluster)
- **HTTP ingress (Traefik)**: `http://home.server:30080/`
  - Private: Tailscale/VPN hits `home.server:30080` directly.
- **Traefik dashboard (NodePort)**: `http://home.server:31080/dashboard`
  - Also reachable via `http://home.server:30080/dashboard` (path-routed), depending on your threat model.
- **PostgreSQL (TCP via Traefik)**: `home.server:32080`
- **Redis (TCP via Traefik)**: `home.server:32081`

### HTTP routing (path/host-based)

```mermaid
flowchart TB
  subgraph publicChat [Public chat.reyops.com]
    CF[Cloudflare] --> HZTraefik[Hetzner_Traefik]
    HZTraefik -->|"Tailscale to home.server:30080"| TraefikWeb
  end

  subgraph privateAccess [Private Tailscale]
    TailscaleVPN[Tailscale_clients] -->|"home.server:30080"| TraefikWeb
  end

  subgraph Cluster
    TraefikWeb[Home_Traefik_NodePort_30080]

    Web[web_default]
    WebCrawler[web-crawler_default]
    Renderer[renderer_default]
    Ollama[ollama_default]
    Dozzle[dozzle_observability]
    Grafana[grafana_observability]
    OpenWebUI[openwebui_default]
  end

  TraefikWeb -->|"Host www.reyops.com, PathPrefix /"| Web
  TraefikWeb -->|"Host www.reyops.com, PathPrefix /crawler (priority 200)"| WebCrawler
  TraefikWeb -->|"PathPrefix /renderer"| Renderer
  TraefikWeb -->|"PathPrefix /ollama"| Ollama
  TraefikWeb -->|"PathPrefix /logs"| Dozzle
  TraefikWeb -->|"PathPrefix /grafana"| Grafana

  TraefikWeb -->|"Host webui.home.server OR chat.reyops.com"| OpenWebUI
  TraefikWeb -->|"Host www.reyops.com, PathPrefix /webui (legacy)"| OpenWebUI
```

### TCP routing (databases)

```mermaid
flowchart LR
  Client[Client] -->|"home.server:32080"| TraefikPostgres[Traefik_TCP_postgres]
  Client -->|"home.server:32081"| TraefikRedis[Traefik_TCP_redis]

  TraefikPostgres -->|"IngressRouteTCP entrypoint postgres"| PostgresSvc[postgres_shared:5432]
  TraefikRedis -->|"IngressRouteTCP entrypoint redis"| RedisSvc[redis_shared:6379]
```

### Routing priorities (important)
- **`/crawler` beats `/`** on `www.reyops.com` due to higher Traefik priority (crawler 200 vs web 100).
- A **lowest-priority catch-all** keeps local/VPN access working even without host headers (`PathPrefix(/)` priority 1).

## 📍 Live Portfolio

**Web (Nuxt SSR)**: https://reyops.com

This live site showcases the infrastructure and provides links to:
- Professional profile and skills
- Technical demonstrations
- LinkedIn & Upwork profiles
- This repository

## 🏗️ Architecture Highlights

### 1. Web Crawler Service
- Async Python + FastAPI architecture
- PostgreSQL persistence with caching
- Redis-based performance optimization
- Concurrent task processing
- Vision extraction endpoint (`/extract-vision`) via Renderer + Ollama
- Memory backoff for resource protection
- Optional robots.txt enforcement
- URL filtering (allowed_domains, exclude_patterns)

### 2. Renderer Service (Playwright-as-a-Service)
- Containerized browser automation
- Screenshot generation API
- HTML rendering endpoint
- Production-ready deployment

### 3. Product Search Agent
- LLM-powered data extraction
- Geographic URL validation (UY, AR, BR, CL, CO, PE, EC, MX, US, ES)
- Multi-phase pipeline: query generation → Brave Search → geo validation → URL extraction → crawling → product classification → price extraction
- Vision fallback for price extraction when text is insufficient
- Redis caching for geo decisions, page type, relevance, prices
- Multi-source aggregation and intelligent analysis

### 4. Observability Stack
- Prometheus metrics collection
- Loki log aggregation (~91 focused sources)
- Grafana dashboards (4 custom dashboards)
- Alert management

### 5. Open WebUI Tools Proxy
- ClusterIP service for Open WebUI tool integrations
- Forwards `/crawl`, `/render-html`, `/screenshot`, `/extract-vision` to web_crawler and renderer
- Enables Open WebUI to use crawler and renderer without direct cluster access

### 6. Shared Infrastructure
- PostgreSQL with Alembic migrations
- Redis with connection pooling
- Repository pattern implementation
- Centralized logging system
- Install path: `shared/shared/` (`pip install -e shared/shared`)
- Service clients: WebCrawlerClient, RendererClient, OllamaClient, RedisClient

## 💼 Professional Context

**Author**: Nelson Fabian Rey  
**Role**: Solutions Architect | Senior Full Stack Developer  
**Location**: Montevideo, Uruguay

This infrastructure represents real-world implementation of:
- Cloud-native architecture patterns
- Microservices design
- DevOps best practices
- Security-first development
- Production operations

## 🔗 Professional Links

- **LinkedIn**: [Nelson Fabian Rey](https://www.linkedin.com/in/nelson-fabian-rey-5177027/)
- **Upwork**: [Freelance Profile](https://www.upwork.com/freelancers/fabianr10)
- **Portfolio**: [reyops.com](https://reyops.com)

## 📚 Repository Structure

```
├── agents/              # AI agents (FastAPI)
│   └── product_search_agent/
├── services/            # Platform services (FastAPI)
│   ├── openwebui_tools/
│   ├── renderer/
│   └── web_crawler/
├── shared/              # Shared python package (install root is shared/shared/)
│   └── shared/
├── k8s/                 # Kubernetes manifests
│   ├── argocd/
│   ├── dozzle/
│   ├── grafana/
│   ├── loki/
│   ├── maintenance/
│   ├── node-exporter/
│   ├── kube-state-metrics/
│   ├── prometheus/
│   ├── promtail/
│   ├── secrets/
│   ├── shared/
│   ├── traefik/
│   ├── web_crawler/
│   ├── renderer/
│   ├── ollama/
│   ├── openwebui/
│   └── openwebui_tools/
├── future_plans/
└── .cursor/rules/
```

The reyops.com web frontend is deployed from a separate infra repository; this repo focuses on agents, crawler, renderer, and observability.

## 🛠️ Technology Showcase

### Backend Engineering
- Python (FastAPI, AsyncIO, SQLAlchemy)
- Node.js & TypeScript
- Repository pattern design
- Async/await architecture

### Cloud & Infrastructure
- Kubernetes (MicroK8s)
- Docker containerization
- GitOps (Kustomize)
- Infrastructure-as-code

### Data Engineering
- PostgreSQL + pgvector
- Redis caching strategies
- Database migrations (Alembic)
- Data pipeline architecture

### AI/ML Integration
- LLM agent development
- Vision model integration
- GPU-accelerated inference
- Intelligent automation

### DevOps & Monitoring
- Prometheus metrics
- Grafana visualization
- Loki log aggregation
- CI/CD practices

### Security
- Zero-trust architecture
- Secret management
- OWASP standards
- Production hardening

## 🎯 Key Features Demonstrated

✅ **Scalable Architecture**: Microservices with proper separation of concerns  
✅ **Production-Ready**: 24/7 uptime with comprehensive monitoring  
✅ **Modern Stack**: Latest technologies and best practices  
✅ **Security-First**: Multiple layers of protection  
✅ **Observable**: Full visibility into system behavior  
✅ **Automated**: Infrastructure-as-code and GitOps workflows  

## 📄 License

This repository serves as a portfolio showcase. The code demonstrates technical capabilities and architectural patterns.

---

**Note**: This is a portfolio demonstration project. While the infrastructure runs production-grade components, it's designed to showcase technical skills and architectural knowledge rather than serve as a production service for external use.

For professional inquiries:
- LinkedIn: https://www.linkedin.com/in/nelson-fabian-rey-5177027/
- Upwork: https://www.upwork.com/freelancers/fabianr10
- Website: https://reyops.com

