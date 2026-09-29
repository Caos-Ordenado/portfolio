# Open WebUI tools releaser: operator-only pilot

`releaser.yaml` is **not** in `portfolio/k8s/kustomization.yaml` or the local
`kustomization.yaml`. Apply this file explicitly only after review. The CronJob
starts suspended. It has no listener, Service, Ingress, or Traefik route. Its
 only in-cluster access is a projected service-account token for named Deployment
 GET/PATCH and named Service GET in `default`. Deployment PATCH can change more
 than the image if the controller code is compromised; treat this reviewed,
 digest-pinned image as high trust. Its other intended traffic is DNS
and public IPv4 HTTPS to GitHub/GHCR. No ConfigMap, app credentials, GitHub
token, or additional Secret is needed. The existing Open WebUI tools Service
remains cluster-internal; this does not expose it over Tailscale or publicly.

**Cluster-wide RBAC is active (verified on home MicroK8s):** the API server now
uses `--authorization-mode=RBAC,Node`, after previously running `AlwaysAllow`.
The named ServiceAccount returns `yes` only for its named Deployment GET/PATCH
and Service GET and `no` for Secrets, other Deployments, lists and pod exec.
Recheck these effective permissions and workload health before any Job, after
every RBAC/addon change. The CronJob remains suspended until an approved pilot
build, image and rollback are verified. Host recovery is documented in the
parent repository's `scripts/caos-host.md` (`sudo microk8s disable rbac`).

## Preflight (no apply)

From the Personal repo root, review the controller source and pinned base image
in `portfolio/services/openwebui_tools_releaser/`. The image is built locally
 on caos for linux/amd64 and imported into MicroK8s; it is **not** pulled from
 GHCR. The manifest pins the reviewed OCI image manifest digest; after building,
 verify the tag's digest with `ctr images ls`, and create the matching digest
 alias in containerd before starting a Job. A reviewed change to the controller
 requires a new manual build/digest and manifest review; never silently reuse
 `pilot-1` for changed source.

```sh
kubectl config current-context # must be microk8s
kubectl -n default get svc kubernetes -o wide # must be 10.152.183.1:443
kubectl -n kube-system get pods -l k8s-app=kube-dns --show-labels
kubectl -n default get deployment openwebui-tools -o yaml # record image AND imagePullPolicy
kubectl -n default get service openwebui-tools -o wide # ClusterIP
kubectl apply --dry-run=server -f portfolio/k8s/openwebui_tools/releaser.yaml
kubectl create --dry-run=client -f portfolio/k8s/openwebui_tools/releaser.yaml -o yaml
```

NetworkPolicy allows CoreDNS pod port 53 UDP/TCP, public IPv4 port 443 (excluding
private, Tailscale/CGNAT and reserved ranges), and the verified Kubernetes API
ClusterIP port 443. Kubernetes Service DNAT / Calico evaluation can differ by
cluster: test actual DNS, GitHub/GHCR and API calls in a **reviewed, manual**
pilot Job while the CronJob remains suspended. If the API ClusterIP rule fails,
stop; investigate with network-policy diagnostics and review any narrowly
scoped node-IP:16443 exception separately. Do not open blanket private egress.
This policy does not provide hostname allowlisting for public HTTPS; anonymous
GHCR availability and the trusted GitHub build workflow remain trust boundaries.

## Staged rollout (requires explicit operator approval)

1. Review `portfolio/.github/workflows/openwebui-tools-deploy.yml` and disable
   any old kubeconfig-based deployment writer **before** starting the pilot;
   coordinate this separately (not changed here). Confirm the trusted workflow
   `build` job and source-only gate remain in force. Ensure the GHCR package
   `ghcr.io/caos-ordenado/openwebui-tools` is anonymously readable: test its
   token and `git-<sha>` manifest from an unauthenticated client and verify the
   controller logs do not report `anonymous_registry_unavailable`. Nodes must
   also be able to pull the selected image digest without an imagePullSecret.
2. Record the current Deployment image and pull policy, and the current rollout
   revision; retain these for rollback. Build/import only after reviewing source:

   ```sh
    scripts/caos-build.sh portfolio/services/openwebui_tools_releaser Dockerfile openwebui-tools-releaser:pilot-1
    ```

    On `caos`, inspect the newly imported tag in MicroK8s containerd and ensure
    its digest equals the manifest's `sha256:9cfc14c2…` before creating the
    digest alias (adjust both when the reviewed source changes):

    ```sh
    /snap/microk8s/current/bin/ctr --address /var/snap/microk8s/common/run/containerd.sock -n k8s.io images ls
    /snap/microk8s/current/bin/ctr --address /var/snap/microk8s/common/run/containerd.sock -n k8s.io images tag \
      docker.io/library/openwebui-tools-releaser:pilot-1 \
      docker.io/library/openwebui-tools-releaser@sha256:9cfc14c271a3783e661ef55f7eb50eae6db4afe921be2e10a15d1595fccabe57
    ```

3. With explicit approval, `kubectl apply -f portfolio/k8s/openwebui_tools/releaser.yaml`.
   Confirm `kubectl -n default get cronjob openwebui-tools-releaser` reports
   SUSPEND=true and that the local image exists on the target node. Check RBAC:

   ```sh
   kubectl auth can-i get deployments.apps/openwebui-tools -n default --as=system:serviceaccount:default:openwebui-tools-releaser
   kubectl auth can-i patch deployments.apps/openwebui-tools -n default --as=system:serviceaccount:default:openwebui-tools-releaser
   kubectl auth can-i get services/openwebui-tools -n default --as=system:serviceaccount:default:openwebui-tools-releaser
   kubectl auth can-i list deployments.apps -n default --as=system:serviceaccount:default:openwebui-tools-releaser # no
   kubectl auth can-i watch deployments.apps -n default --as=system:serviceaccount:default:openwebui-tools-releaser # no
   kubectl auth can-i get secrets -n default --as=system:serviceaccount:default:openwebui-tools-releaser # no
   ```

4. Only **after RBAC is active and negative permission checks return `no`**,
    confirming the expected new app digest and approving that rollout,
   run one pilot Job using `kubectl -n default create job --from=cronjob/openwebui-tools-releaser openwebui-tools-releaser-pilot-1`.
   A Job created manually **runs even while the CronJob is suspended**. Check
   `kubectl -n default logs job/openwebui-tools-releaser-pilot-1` and
   `kubectl -n default get job openwebui-tools-releaser-pilot-1`;
   inspect Deployment, pod health, Service type, and image/policy after it exits.
   Do not unsuspend if networking, anonymous access, policy, or readiness fails.
5. Only after review of pilot results and separate approval for recurring
   rollout, set `spec.suspend=false` by an explicitly reviewed manifest change
   or operator patch. **Do not reapply this suspended file to an enabled CronJob
   without planning for the resulting pause.** The job runs every 10 minutes,
   forbids concurrent scheduled runs, has no retry, and allows 1200 seconds for
   up to 20 bounded GitHub job requests plus two 300-second readiness waits.

## Recovery and drift

Suspend first: `kubectl -n default patch cronjob openwebui-tools-releaser --type=merge -p '{"spec":{"suspend":true}}'`.
Wait for any active Job to finish or explicitly terminate it after incident
review (suspending does not cancel Jobs). Inspect `kubectl -n default get
deployment openwebui-tools -o yaml` and `kubectl -n default rollout history
deployment/openwebui-tools`. If the controller reported
`restore_failed_manual_inspection_required`, compare live image/policy to the
recorded pre-pilot values before intervening; do not overwrite a concurrent
writer. Restore the recorded good image and `imagePullPolicy` together (e.g.
an explicitly reviewed `kubectl -n default patch deployment openwebui-tools
--type=strategic -p '{"spec":{"template":{"spec":{"containers":[{"name":"openwebui-tools","image":"<recorded-good-image>","imagePullPolicy":"<recorded-policy>"}]}}}}'`).
Then `kubectl -n default rollout status deployment/openwebui-tools --timeout=5m`
and check pod readiness. The initial manifest uses a locally imported image
with `Never`; a GHCR digest rollout uses `IfNotPresent`, so restoring the image
without restoring the policy is not a complete rollback. Re-applying the
existing kustomization or the legacy deploy script can also reset the image or
policy: treat that as a competing writer, suspend the controller, reconcile the
intended source of truth, then consider resuming only after review.

Post-change: check CronJob SUSPEND, Job status and logs (no tokens or registry
responses), Deployment image/policy and readiness, pods/events for pull failures,
Service `ClusterIP`, RBAC denials, and NetworkPolicy DNS/API/public HTTPS
reachability. Record the deployed digest and next scheduled result; if the Job
fails, leave suspended and investigate instead of widening RBAC or egress.
