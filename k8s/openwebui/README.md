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

Use the same IP as `home.server` (your Tailscale machine IP). This does not make Open WebUI the main service—Traefik routes by host header; other services remain at `home.server:30080/llm`, `home.server:30080/crawler`, etc.

### Public access (`https://chat.reyops.com/`)

Ingress matches **`Host(chat.reyops.com)`** on **home** Traefik (see `ingress.yaml`). This setup does **not** use Cloudflare Tunnel to the home server; public HTTPS terminates at **Cloudflare → Hetzner**, and Hetzner reaches home over **Tailscale**:

1. **Proxied DNS → Hetzner** (orange-cloud `A`/`AAAA` for `chat` to the Hetzner VPS public IP).
2. On **Hetzner K3s**, apply [`infra/hosting/k3s/reyops/openwebui-chat-proxy.yaml`](../../../infra/hosting/k3s/reyops/openwebui-chat-proxy.yaml): Traefik (`websecure`) forwards to `http://<home_tailscale_ip>:30080`. The browser still sends `Host: chat.reyops.com`, which home Traefik matches to Open WebUI.
3. Edit the **Endpoints** IP in that manifest to your home machine’s **Tailscale** IPv4 (same as `ping home.server` from Hetzner).

If `chat.reyops.com` does not resolve (e.g. `NXDOMAIN`), create the Cloudflare DNS record first.

*Optional elsewhere:* Cloudflare Tunnel to an origin is unrelated to this Tailscale-based path.

- Back-compat: `https://www.reyops.com/webui` (path-based; may have asset issues)

## Troubleshooting: `404` on `https://chat.reyops.com`

Cloudflare DNS being “set” only proves the name resolves; **404 almost always means Traefik on the first hop has no router for `Host: chat.reyops.com`**, or the hop after that cannot match Open WebUI.

### 1) See which layer returns 404

```bash
curl -sSI https://chat.reyops.com/
```

Note `server` / `cf-ray` (Cloudflare) vs `404` body (Traefik often shows a short “404 page not found” from Traefik itself).

### 2) Hetzner K3s (most common gap)

[`web-reyops-ingress`](../../../infra/hosting/k3s/reyops/ingress.yaml) only matches `reyops.com` and `www.reyops.com`. **`chat.reyops.com` needs a separate IngressRoute** — apply on the **Hetzner** cluster (not home):

```bash
# After editing Endpoints IP to your home Tailscale IPv4:
kubectl apply -f infra/hosting/k3s/reyops/openwebui-chat-proxy.yaml
kubectl get ingressroute -n default
kubectl describe ingressroute openwebui-chat-reyops -n default
```

If `openwebui-chat-reyops` is missing, Traefik serves **404** for `chat.reyops.com`.

### 3) API group on K3s Traefik

If the resource never appears or Traefik ignores it, check CRD group:

```bash
kubectl api-resources | grep -i ingressroute
```

Use `traefik.io/v1alpha1` vs `traefik.containo.us/v1alpha1` to match your cluster (edit the manifest `apiVersion` if needed).

### 4) Backend reachability (wrong IP → often 5xx, not 404)

From the Hetzner node:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -H "Host: chat.reyops.com" "http://<HOME_TAILSCALE_IP>:30080/"
```

Expect **200** (or redirect) once home Traefik matches `Host(chat.reyops.com)` (`kubectl apply -k k8s/openwebui` on **home**).

### 5) Home Microk8s

```bash
kubectl get ingressroute openwebui-ingress -n default -o yaml | grep -A2 chat
```

If the **chat.reyops.com** host rule is missing from `openwebui-ingress`, apply `k8s/openwebui` on home.

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
   - `WEBUI_SECRET_KEY` is generated automatically via `__SERVER_SECRET_KEY__`

`WEBUI_URL` is pre-set to **`https://chat.reyops.com/`** for OAuth/SSO and public links. Use private `http://webui.home.server:30080/` in the browser when on Tailscale without public DNS.

## 3) Deploy

Open WebUI uses a pinned image and `Recreate` rollout strategy: startup migrations must not run alongside an older Open WebUI pod. Before changing the image, take a verified `pg_dump -Fc` of the `openwebui` database and back up any contents of `/app/backend/data/uploads` and `/app/backend/data/vector_db`. The current deployment does not mount a volume at `/app/backend/data`, so files there do not survive a pod replacement. The embedding-model cache there can be downloaded again.

Apply manifests from the `portfolio/` directory on the **home MicroK8s** context:

```bash
kubectl apply -k k8s/openwebui
```

The OpenAI-compatible llama-swap connection exposes only the stable `coder`, `extract`, `reasoning` and `vision` aliases in Open WebUI. llama-swap itself also publishes the underlying model IDs with the same display names, so listing both produces duplicates. Open WebUI persists connection settings in PostgreSQL; if an existing installation already has an `openai.api_configs` value, update that connection's **Model IDs** in Admin → Connections as well (or the stored value will override `OPENAI_API_CONFIGS` on restart).

Web search is enabled with the built-in `ddgs` provider. It requires outbound internet access from the Open WebUI pod; searches send the query to an external provider. Web search is separate from the `openwebui-tools` OpenAPI server (crawler, renderer, screenshot, vision extraction): the latter is an admin-configured global integration. Check Admin → Integrations for its connection; to use it in a chat, open **Integrations → Tools** beside the message box and enable it for that chat. Global tool servers are hidden in the chat until explicitly enabled. Web search also needs to be enabled for the selected model/chat.

Access policy observed after this upgrade: public sign-up is enabled, but new accounts have the `pending` role until an admin approves them. The public `/api/config` confirms sign-up is visible. If open registration is not desired, change **Admin → Authentication → Sign Up** and verify `/api/config` from an unauthenticated browser; that persistent admin setting takes precedence over a manifest environment variable.

## 4) Verify

- Check the UI (private): `http://webui.home.server:30080/`
- Check `kubectl rollout status deployment/openwebui -n default` and `kubectl logs deployment/openwebui -n default` for migration errors.
- Check `http://webui.home.server:30080/api/version` and `https://chat.reyops.com/`.
- Sign in and confirm existing chats, exactly four model aliases, a real web search, and a tool call through the enabled global integration. The tools proxy's OpenAPI spec is available **inside the cluster** at `http://openwebui-tools.default.svc.cluster.local:8000/openapi.json`.

After a home-server reboot, Open WebUI can briefly exit while CoreDNS/PostgreSQL
recover (`Temporary failure in name resolution` for the database). It then
downloads the embedding-model cache into its unmounted `/app/backend/data`
again. The pod has no ready endpoint during this work, so both ingress URLs may
return `503` for several minutes. Check CoreDNS and Postgres readiness and
`kubectl logs deployment/openwebui -n default`; wait for the pod to be `1/1`,
then allow Traefik a few seconds to observe the new endpoint before diagnosing
an ingress failure. Repeated rollout restarts discard the in-progress cache.

### Rollback after a schema migration

Changing the image back is insufficient if the new version migrated PostgreSQL. For the v0.6.43 → v0.11.4 update, the verified private backups are in `~/.local/share/openwebui-backups/` on the operator's machine (`openwebui-pre-v0.11.4-20260927.dump` and `openwebui-data-pre-v0.11.4-20260927.tar`; **never** add these to git). The local `pg_restore` may be older than the server's dump format; use the PostgreSQL container's `pg_restore` and stream the archive into it. From the repository root with `kubectl` pointing at `microk8s`:

```bash
kubectl scale deployment/openwebui -n default --replicas=0
kubectl rollout status deployment/openwebui -n default --timeout=120s
# Stop new connections and rebuild the database, removing migrated-only tables.
kubectl exec -n shared deployment/postgres -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -v ON_ERROR_STOP=1 -U admin -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '\''openwebui'\'' AND pid <> pg_backend_pid()"'
kubectl exec -n shared deployment/postgres -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" dropdb -U admin openwebui && PGPASSWORD="$POSTGRES_PASSWORD" createdb -U admin -O openwebui_user openwebui'
kubectl exec -i -n shared deployment/postgres -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --exit-on-error -U admin -d openwebui' < "$HOME/.local/share/openwebui-backups/openwebui-pre-v0.11.4-20260927.dump"
kubectl set image deployment/openwebui -n default openwebui=ghcr.io/open-webui/open-webui:v0.6.43
kubectl scale deployment/openwebui -n default --replicas=1
kubectl rollout status deployment/openwebui -n default --timeout=900s
```

Before a rollback, also revert the pinned image in `deployment.yaml` to v0.6.43 so the next apply does not re-upgrade it. The archived `uploads/` and `vector_db/` were empty of user content for this update (the SQLite vector database had zero collections); if a later update contains local data, restore it to a **persistent** `/app/backend/data` volume before restarting. Verify login, chats and both ingress URLs. Restoring the database discards changes made since the backup.

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
