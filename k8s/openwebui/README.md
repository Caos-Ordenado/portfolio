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
   - `__OLLAMA_BASE_URL__` (recommended: `http://ollama.default.svc.cluster.local:11434`)
   - `WEBUI_SECRET_KEY` is generated automatically via `__SERVER_SECRET_KEY__`

`WEBUI_URL` is pre-set to **`https://chat.reyops.com/`** for OAuth/SSO and public links. Use private `http://webui.home.server:30080/` in the browser when on Tailscale without public DNS.

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
