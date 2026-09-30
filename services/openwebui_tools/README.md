# Open WebUI Tools Proxy

Small FastAPI service that exposes **stable HTTP endpoints** for Open WebUI “tools”, and forwards calls to:

- Web Crawler (`web-crawler` service)
- Renderer (`renderer` service)

This service is designed to run **inside the cluster** (ClusterIP only).

## Endpoints

- `GET /health` → `{ "status": "ok" }`
- `POST /crawl` → forwards to crawler `POST /crawl`
- `POST /render-html` → forwards to renderer `POST /render-html`
- `POST /screenshot` → forwards to renderer `POST /screenshot`
- `POST /extract-vision` → forwards to crawler `POST /extract-vision`

All tool endpoints return a normalized shape:

```json
{ "success": true, "data": { ... }, "error": null }
```

On failure:

```json
{ "success": false, "data": null, "error": "..." }
```

## Configuration

Environment variables:

- `CRAWLER_BASE_URL` (default: `http://web-crawler.default.svc.cluster.local:8000`)
- `RENDERER_BASE_URL` (default: `http://renderer.default.svc.cluster.local:8000`)
- `LOG_LEVEL` (default: `INFO`)
- `OPENWEBUI_TOOLS_HTTP_TIMEOUT_SECONDS` (default: `180`) — proxy timeout for upstream calls (increase for `/extract-vision`)

## Local run

```bash
cd services/openwebui_tools
./start.sh
```

## Container dependency lock

The Linux amd64 container uses a reviewed Python 3.13 slim image digest and installs
only wheel distributions pinned with hashes in `requirements.lock`. The lock is the
runtime dependency union of `shared/shared/pyproject.toml` and this service's
`pyproject.toml` (excluding dev extras). Local source is imported through
`PYTHONPATH`, not installed with editable pip/build dependencies. When either
project's runtime requirements change, review and regenerate the lock from the
`portfolio/` directory with `uv`:

```bash
uv pip compile shared/shared/pyproject.toml services/openwebui_tools/pyproject.toml \
  --python-version 3.13 --python-platform x86_64-manylinux_2_17 \
  --only-binary :all: --generate-hashes \
  --no-emit-package shared --no-emit-package openwebui-tools --no-annotate \
  -o services/openwebui_tools/requirements.lock
```

Review the resolved versions and build the image with the existing context layout:
`shared/` contains the shared package and `openwebui_tools/` contains this service.
The committed-only CI context must explicitly archive
`services/openwebui_tools/requirements.lock` alongside the Dockerfile and source;
otherwise Docker's `COPY` fails. The CI workflow archive and its source-only deploy
gate are owned separately from this service; coordinate their update before release.

## Home-server releases

For source-only changes to `src/`, the App opens a PR, GitHub requires the `checks`
job and an owner review, and hosted Actions publishes a public GHCR image with a
run-bound digest. The in-cluster `openwebui-tools-releaser` CronJob (every 10
minutes) verifies the current `main` build, its digest status, and GHCR before
updating this **ClusterIP-only** Deployment. The reviewed one-shot and recurring
rollouts, health check, and manual rollback to the previous local image passed.
See [`k8s/openwebui_tools/RELEASER.md`](../../k8s/openwebui_tools/RELEASER.md)
for current image, RBAC, observability, and rollback commands. The checked-in
legacy Deployment image is not the live controller-managed digest: suspend the
CronJob and reconcile the desired image/pull policy before running `./deploy.sh`
or `kubectl apply -k k8s/openwebui_tools`, since either may overwrite it.
