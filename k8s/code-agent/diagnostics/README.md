# Cluster diagnostics (opt-in overlay; live at one replica)

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

## Live post-rollout state (PR #39/#40, home MicroK8s)

Diagnostics, `open-terminal`, `infra-terminal`, `code-agent-pr-broker`, and
`infra-pr-broker` were observed 1/1. Diagnostics runs the pinned image
`sha256:589490b0a26f1a1914e494a34453edd078da218a2b16f5371c14c8284970e2b5`
at one replica; its TCP 8002 ClusterIP was `10.152.183.59` (recheck after
recreation). Only the two same-namespace terminal Pods could ingress, using
independent bearer keys. The Kubernetes API CA chain and Service IP identity
validated with only X.509 STRICT relaxed for the MicroK8s CA keyUsage issue.
Cross-namespace pods/events/metrics summaries and Dozzle pod logs worked.
RBAC denied Secrets, ConfigMaps, exec, proxy and writes; unrelated Pod ingress,
public/unrelated-service diagnostics egress, and terminal direct API access
were denied. No new public route was added. These are rollout observations,
not guarantees against later CNI/policy drift.

**Drift warning:** this standalone checked-in overlay deliberately sets
`replicas: 0`, whereas live is 1. Reapplying it with
`kubectl apply -k k8s/code-agent/diagnostics` scales the live service to zero.
Do not use that command as routine live reconciliation.

## Rollout (manual, only after reviewed PR)

1. The pinned diagnostics image was built/imported locally on caos. Verify its
   digest matches the manifest; then check the non-root read-only runtime,
   Kubernetes CA validation against the API Service IP without DNS, and
   `imagePullPolicy: Never` on the target node. Keep replicas at 0 until
   these checks pass on a fresh install; live was subsequently scaled to 1.
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

Scale diagnostics to zero first with
`kubectl scale deployment/cluster-diagnostics -n code-agent --replicas=0`.
Restore both terminal Deployments **and** NetworkPolicies from the pre-change
snapshot or reviewed prior revision (remove their diagnostics key dependency
and TCP 8002 egress together); delete only the standalone
diagnostics overlay's resources by exact name (Service, Deployment, NetworkPolicy,
ServiceAccount, ClusterRoleBinding, ClusterRole), not by deleting the namespace
or applying the parent. Verify no diagnostics endpoint or RBAC binding remains.

## Read-only health / post-change checks

Run against the intended home MicroK8s context; do not print keys or raw logs.
Expect diagnostics, both terminals and both brokers at 1/1; a ready diagnostics
endpoint; the pinned image and live replica count 1; ClusterIP TCP 8002 (IP
`10.152.183.59` at rollout); and no new public ingress:

```sh
kubectl config current-context
kubectl -n code-agent get deployments cluster-diagnostics open-terminal infra-terminal code-agent-pr-broker infra-pr-broker
kubectl -n code-agent get pods -l app=cluster-diagnostics -o wide
kubectl -n code-agent get svc cluster-diagnostics -o wide
kubectl -n code-agent get endpoints cluster-diagnostics
kubectl -n code-agent get deployment cluster-diagnostics -o jsonpath='{.spec.replicas}{" replicas; image="}{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n code-agent get networkpolicy cluster-diagnostics-isolation open-terminal-isolation infra-terminal-isolation
kubectl -n code-agent get ingress
kubectl -n code-agent get ingressroutes.traefik.io
```

Compare public exposure with the pre-change baseline; an internal ClusterIP
alone does not establish that no external route was added. Check RBAC with
impersonation (an impersonation-permission error is inconclusive):

```sh
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics list pods --all-namespaces
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics list events --all-namespaces
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics list pods.metrics.k8s.io --all-namespaces
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics get pods --subresource=log --all-namespaces
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics get secrets --all-namespaces
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics get configmaps --all-namespaces
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics create pods --subresource=exec -n code-agent
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics get pods --subresource=proxy -n code-agent
kubectl auth can-i --as=system:serviceaccount:code-agent:cluster-diagnostics create deployments.apps -n code-agent
```

First four should say `yes`, last five `no`. These do not retest HTTP bearer
authentication or live CNI isolation. After policy/CNI/key changes repeat
controlled in-cluster checks from both permitted terminals and an unrelated
Pod: the unauthenticated `/ready` probe and authenticated bounded resources
and Dozzle logs work only through
the terminals; unrelated ingress, unrelated/public diagnostics egress, and
terminal direct API traffic fail. There is no committed read-only network
probe command that reproduces those live checks without a controlled
in-cluster session; treat inability to repeat them as a verification gap, not
a pass. Never publish keys, raw logs or `/logs` responses as evidence.

## Recovery / remaining exposure

If readiness or endpoint disappears, check the desired/ready counts, image,
Service endpoint and API VIP/endpoint:

```sh
kubectl get svc kubernetes -n default -o wide
kubectl get endpoints kubernetes -n default -o wide
```

If API reachability regresses, inspect CNI policy and Service DNAT ordering for the
scoped VIP/backing endpoint; never add blanket `0.0.0.0/0` egress or disable
TLS validation. If a reapply accidentally sets replicas to 0, only after
reviewing policies/image/key references restore the explicitly approved live
count and check rollout plus endpoints:

```sh
kubectl scale deployment/cluster-diagnostics -n code-agent --replicas=1
kubectl rollout status deployment/cluster-diagnostics -n code-agent
kubectl -n code-agent get endpoints cluster-diagnostics
```

If isolation or bearer checks fail, stop using
the bridge and follow Rollback above rather than widening access.

Raw logs can contain credentials or private values and enter the configured
model context; the owner explicitly accepted this flow. Never publish raw
logs in a public PR. The owner also accepted that the portfolio terminal's
existing public HTTPS and public PR capabilities could disclose log-derived
text before review; this design does not guarantee otherwise.
