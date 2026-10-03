# Cluster diagnostics (opt-in, zero replicas by default)

Standalone overlay; neither the parent nor portfolio root kustomization includes it.
Cluster-internal ClusterIP TCP 8002 only; no Ingress, NodePort, Tailscale route or
public access. Only same-namespace `open-terminal` and `infra-terminal` pods may
connect. Each terminal also sends its own 64-hex bearer key from the private
operator-created `cluster-diagnostics-api-keys` Secret; never put key values
in git or command arguments. The terminal policies add TCP 8002 egress to the diagnostics pod only.
No DNS or public egress from diagnostics. The diagnostic Pod alone mounts its
ServiceAccount token. The Service name injects `CLUSTER_DIAGNOSTICS_SERVICE_HOST`
into the DNS-less infra terminal when the Service exists before terminal startup;
restart that terminal after creating the Service if necessary. RBAC is read-only
across namespaces and intentionally excludes Secrets and ConfigMaps.

## Rollout (manual, only after reviewed PR)

1. The pinned diagnostics image was built/imported locally on caos. Verify its
   digest matches the manifest; then check the non-root read-only runtime,
   Kubernetes CA validation against the API Service IP without DNS, and
   `imagePullPolicy: Never` on the target node. Keep replicas at 0 until
   these checks pass.
   Python 3.13 uses strict X.509 checks that reject the existing MicroK8s CA's
   missing keyUsage; this service disables only `VERIFY_X509_STRICT` while
   retaining CA and API service IP identity checks. Do not set `CERT_NONE` or
   disable hostname validation to work around a different TLS error.
   Create `cluster-diagnostics-api-keys` out of git with independent random
   `PORTFOLIO_KEY` and `INFRA_KEY` values (64 hex characters each, no newline),
   supplied from operator-owned mode-0600 files. Verify the Secret exists by
   name only; never print or commit its data. Create it **before** applying
   either terminal Deployment, since both now reference their own key.

   ```sh
   umask 077
   PORTFOLIO_FILE=$(mktemp)
   INFRA_FILE=$(mktemp)
   openssl rand -hex 32 | tr -d '\n' > "$PORTFOLIO_FILE"
   openssl rand -hex 32 | tr -d '\n' > "$INFRA_FILE"
   kubectl -n code-agent create secret generic cluster-diagnostics-api-keys \
     --from-file=PORTFOLIO_KEY="$PORTFOLIO_FILE" \
     --from-file=INFRA_KEY="$INFRA_FILE"
   rm -f "$PORTFOLIO_FILE" "$INFRA_FILE"
   ```

   Keep shell tracing off and verify only the two key **names** in the Secret;
   no token or full Secret YAML belongs in logs or tickets. Rotate deliberately
   and restart both terminal Pods and diagnostics if either key changes.
2. On home MicroK8s confirm `kubectl config current-context`,
   `kubectl get svc kubernetes -n default -o wide`, and
   `kubectl get endpoints kubernetes -n default -o wide`. This overlay pins the
   observed VIP `10.152.183.1:443` and endpoint `192.168.68.6:16443`;
   re-review policy if either changes. Check whether the CNI evaluates policy
   before or after Service DNAT; both destinations are deliberately scoped.
3. From `portfolio/`, review `kubectl kustomize k8s/code-agent/diagnostics` and
   `kubectl apply --dry-run=server -k k8s/code-agent/diagnostics`.
   Snapshot existing policies and deployment spec before touching them.
4. After approved PR, apply this standalone overlay with
   `kubectl apply -k k8s/code-agent/diagnostics` then apply the two
   terminal policies by exact file path. Never apply the portfolio root to
   activate this overlay. Verify the Service link in a restarted infra terminal.
   Only after the pinned image and network/RBAC checks, explicitly scale
   `kubectl scale deployment/cluster-diagnostics -n code-agent --replicas=1`;
   `kubectl rollout status deployment/cluster-diagnostics -n code-agent`.

## Rollback

Scale diagnostics to zero first. Restore both terminal NetworkPolicies from
the pre-change snapshot or reviewed prior revision; delete only the standalone
diagnostics overlay's resources by exact name (Service, Deployment, NetworkPolicy,
ServiceAccount, ClusterRoleBinding, ClusterRole), not by deleting the namespace
or applying the parent. Verify no diagnostics endpoint or RBAC binding remains.

## Post-change / negative checks

- `kubectl get pods,svc,endpoints,networkpolicy -n code-agent` and
  `kubectl get clusterrole,clusterrolebinding code-agent-cluster-diagnostics`;
  verify no diagnostic Pod before deliberate activation and no public Service.
- `kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics
  list pods --all-namespaces` should be yes; `get pods --subresource=log` should
  be yes.
  `get secrets --all-namespaces`, `get configmaps --all-namespaces`,
  `create pods`, `create pods --subresource=exec`, and
  `get pods --subresource=proxy` should all be no. Always pass `--subresource`
  explicitly; `kubectl auth can-i get pods/proxy` can be parsed as a resource
  name instead of a subresource check and give a misleading result.
- From both allowed terminals test `http://$CLUSTER_DIAGNOSTICS_SERVICE_HOST:8002/health`
  (the open terminal may use the Service ClusterIP if its Service link is absent).
  From any other pod, test port 8002 is blocked. From diagnostics confirm the
  API Service VIP works and logs show no DNS/public access; test DNS, public
  TCP 443 and unrelated RFC1918 endpoints are denied. If API access fails,
  inspect CNI policy/DNAT translation for the Service VIP and backing endpoint,
  not a blanket `0.0.0.0/0` exception. NetworkPolicy enforcement and the
  diagnostic server's bearer-key authentication and response shape require live review.
   Raw logs may contain credentials or private values and enter the configured
   model context; the owner explicitly chose this data flow. Never publish raw
   logs in a public PR. The owner separately accepted that the portfolio
   terminal's existing public HTTPS and public PR capabilities could disclose
   log-derived text before review; this design does not guarantee otherwise.
