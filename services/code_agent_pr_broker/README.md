# Code agent PR broker

Private/internal FastAPI service accepting `POST /proposals` and `GET /health`. Do **not** expose it to the public Internet: the broker does not authenticate callers; access must be limited to the trusted coding pod by deployment/network policy. The coding pod receives only the broker URL, never GitHub credentials. It creates PRs against `Caos-Ordenado/portfolio` `main` from random `agent/openwebui-tools/` branches.

Configure `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, and `GITHUB_APP_PEM_PATH` (path to a read-only mounted Secret, **not** PEM contents). Grant the App installation only `portfolio`, `contents:write` and `pull_requests:write`. Keep the private key and installation token out of logs and the terminal pod. No credentials are baked into the image. `GET /health` does not check GitHub credentials.

Run locally with `pip install -e '.[test]'` then `uvicorn broker.main:app --host 127.0.0.1 --port 8001`; run `pytest -q tests/test_broker.py` for mocked, offline verification. Build from this directory with `docker build -t code-agent-pr-broker .`. Deployment and network isolation are supplied by `k8s/code-agent/`.

Request JSON: `{"title":"...","body":"...","message":"...","files":[{"path":"services/openwebui_tools/src/example.py","content":"..."}]}`. Paths must be under that source directory, regular `.py` files only. At most 20 distinct files and 64 KiB total UTF-8 content; body capped at 256 KiB. Unknown fields (including repository/ref/URL overrides), dotfiles, traversal, symlinks, no-op proposals and truncated GitHub trees are rejected. GitHub errors return a generic error; an orphan branch may require manual cleanup if PR creation and cleanup both fail.

## Autonomous publication policy

An authorized admin terminal model may submit proposed source files to this broker. A valid proposal **immediately opens a PUBLIC GitHub PR visible before human review**; there is no approval step before PR publication. CI and human reviews gate **merge and deployment**, not PR creation. Treat titles, descriptions and file contents as public at submission time; do not send secrets or private data. The path and size caps still apply.

## Reproducible image dependencies

The Dockerfile pins the Linux amd64 Python base image by its platform-specific manifest digest (checked with `docker buildx imagetools inspect python:3.13-slim`). Runtime dependencies, including transitive dependencies, are version- and SHA-256-locked in `requirements.lock`. The Docker build uses `pip --require-hashes --only-binary` and copies the app source directly; it does not resolve or install the local package or pull unpinned build dependencies. Build for `linux/amd64` only.

To refresh after reviewing dependency updates, run from this directory:

```bash
uv pip compile pyproject.toml --python-version 3.13 --python-platform x86_64-unknown-linux-gnu --only-binary :all: --generate-hashes --no-header --no-annotate --output-file requirements.lock
docker build --platform linux/amd64 -t code-agent-pr-broker .
pytest -q tests/test_broker.py
```

Review **every** resolved version and hash, then verify the build and tests before accepting a lock refresh. Reinspect and deliberately update the base digest when upgrading Python. `requirements.lock` is runtime-only; the optional `test` extra is not included in the image.
