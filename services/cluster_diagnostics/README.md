# Read-only cluster diagnostics for coding terminals

The internal `cluster-diagnostics` service lets both coding terminals inspect
resource **summaries** and recent pod logs across all namespaces without giving
either terminal Kubernetes credentials. The service alone holds a projected
ServiceAccount token. Each terminal has its own operator-created diagnostic
HTTP bearer key, separate from Kubernetes and the GitHub App. It has no
Kubernetes Secret, ConfigMap, exec, proxy, mutation or
deployment permission. Its ClusterIP is reachable only from the two terminals
on TCP 8002; its egress is limited to the Kubernetes API. No public endpoint.

`GET /kinds` lists supported resources. Use `GET /resources?kind=pods` with
optional `namespace`, `limit` (1–100), and returned `continue` token for further
pages. `GET /logs?namespace=observability&pod=NAME&tail=100` accepts optional
`container` and `previous=true`; each response is capped at 300 lines and 64 KiB.
`GET /ready` checks Kubernetes API reachability. No arbitrary Kubernetes API
path, POST, Secret object, environment values or raw Pod spec is returned.
Construct the `Authorization: Bearer` request header **inside** a Python client
using `os.environ["CLUSTER_DIAGNOSTICS_KEY"]`; never put the expanded token in
`curl -H`, a process argument, a URL or diagnostic logs. Each key is restricted
to this internal HTTP service, not the Kubernetes API.
Pod logs and event messages can themselves contain sensitive values; do not
paste raw logs into public PRs. The owner explicitly authorized raw log lines
to enter the agent/model context for diagnosis; this can disclose data present
in logs to the configured model provider. The owner separately accepted that
the portfolio terminal could publish log-derived text in a public PR or send it
over its existing public HTTPS egress; this bridge does not guarantee otherwise.
Event summaries omit free-text messages; raw pod logs are returned unchanged,
capped per request.
When the owner explicitly requests logs in chat, both terminal agents may
display those returned lines verbatim, even if they contain sensitive values.
This permission is for the owner's chat, not a public PR or diagnostic bearer
key disclosure. The service's per-request bounds remain in force.

## Live status after PR #39/#40

On home MicroK8s, diagnostics, `open-terminal`, `infra-terminal`, and their
`code-agent-pr-broker` / `infra-pr-broker` Deployments were observed 1/1.
Diagnostics uses image digest
`sha256:589490b0a26f1a1914e494a34453edd078da218a2b16f5371c14c8284970e2b5`
and TCP 8002 ClusterIP `10.152.183.59` (the IP may change if recreated).
Only the two terminal Pods ingressed with separate keys; the diagnostics Pod
alone called the Kubernetes API. CA-chain and API Service IP identity were
validated with X.509 STRICT relaxed only for the home MicroK8s CA's missing
keyUsage. Cross-namespace pod, event and metric summaries and Dozzle pod logs
worked; RBAC denied Secrets, ConfigMaps, exec, proxy and writes. Live checks
denied unrelated-pod ingress, unrelated/public diagnostics egress and terminal
direct API access. No new public route was added.

`/health` is liveness only; `/ready` actually queries the API. Check readiness,
endpoint, image and negative RBAC permissions with the read-only commands in
`../../k8s/code-agent/diagnostics/README.md`; they cannot independently prove
bearer authentication or CNI isolation after a change. On readiness failure,
check the API Service VIP and scoped policy/DNAT paths before changing network
rules or TLS. The checked-in overlay intentionally specifies zero replicas
while live is one: reapplying it scales diagnostics down. For a failed
isolation check, stop using the bridge and follow the overlay rollback (scale
diagnostics to zero, restore **both** terminal Deployments and policies).
Do not print bearer keys or raw log responses while investigating.

Run offline tests from this directory with `python3 -m pytest -q tests`.
The image uses only Python's standard library and a digest-pinned Python base.
Deployment, rollback, isolation and negative checks are in
`../../k8s/code-agent/diagnostics/README.md`.
