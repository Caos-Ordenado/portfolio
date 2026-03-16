# Open WebUI (Kubernetes)

This directory deploys **Open WebUI** into the `default` namespace and exposes it via Traefik.

## Access (host-based routing)

Open WebUI uses root-level paths (`/api`, `/ws`, `/_app`, `/assets`) and does **not** support a configurable subpath. It must be reached via **host-based routing**.

### Private access (Tailscale/VPN)

**URL**: `http://webui.home.server:30080/`

**Required**: Add to your `/etc/hosts` (or local DNS):

```
<TAILSCALE_IP>  webui.home.server
```

Use the same IP as `home.server` (your Tailscale machine IP). This does not make Open WebUI the main service—Traefik routes by host header; other services remain at `home.server:30080/ollama`, `home.server:30080/crawler`, etc.

### Public access

- Cloudflare tunnel: `https://chat.reyops.com/`
- Back-compat: `https://www.reyops.com/webui` (path-based; may have asset issues)

## 1) PostgreSQL provisioning (one-time)

Open WebUI must use PostgreSQL (no SQLite persistence).

Connect as Postgres admin and run:

```sql
CREATE USER openwebui_user WITH PASSWORD '<generated-password>';
CREATE DATABASE openwebui OWNER openwebui_user;
GRANT ALL PRIVILEGES ON DATABASE openwebui TO openwebui_user;
```

### Suggested `DATABASE_URL`

Use the in-cluster Postgres service DNS:

`postgresql://openwebui_user:<password>@postgres.shared.svc.cluster.local:5432/openwebui`

## 2) Secrets (required)

Generate and apply `openwebui-secrets` using the repo secret generator:

1. Run `k8s/secrets/scripts/generate-secrets.sh`
2. Select the `openwebui.template.yaml` template
3. Provide values for:
   - `__OPENWEBUI_DATABASE_URL__` (see above)
   - `__OLLAMA_BASE_URL__` (recommended: `http://ollama.default.svc.cluster.local:11434`)
   - `WEBUI_SECRET_KEY` is generated automatically via `__SERVER_SECRET_KEY__`

`WEBUI_URL` is pre-set to `http://webui.home.server:30080/` for OAuth/SSO redirects.

## 3) Deploy

Apply manifests:

```bash
kubectl apply -k k8s/openwebui
```

## 4) Verify

- Check the UI (private): `http://webui.home.server:30080/`
- Confirm it persists to Postgres (restart the pod and ensure state remains).

## 5) Reset admin password

If you cannot log in, reset the admin password:

```bash
./k8s/openwebui/scripts/reset-admin.sh [admin_email]
```

Run from the repo root. Connects to postgres at `home.server:32080` (override with `POSTGRES_HOST` / `POSTGRES_PORT`). Requires `psql`, project `.venv` (bcrypt), and Tailscale/VPN access to home.server.

List users in the database:

```bash
./k8s/openwebui/scripts/reset-admin.sh --list
```

If no users exist, create one via the sign-up page, or add `WEBUI_ADMIN_EMAIL` and `WEBUI_ADMIN_PASSWORD` to the deployment for first-run admin creation.
