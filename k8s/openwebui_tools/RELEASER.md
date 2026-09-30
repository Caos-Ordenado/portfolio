# Open WebUI tools releaser (home MicroK8s)

`releaser.yaml` is **not** in `portfolio/k8s/kustomization.yaml` or the local
`kustomization.yaml`. Apply this file explicitly only after review. The CronJob
was staged suspended for the pilot; its reviewed manifest now enables recurrence
every 10 minutes. It has no listener, Service, Ingress, or Traefik route. Its
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
Recheck these effective permissions and workload health after every RBAC/addon
change. The approved pilot build, image rollout and manual rollback drill passed:
the Deployment returned to the GHCR digest with `IfNotPresent`, ready 1/1 and
`/health` working. Host recovery is documented in the
parent repository's `scripts/caos-host.md` (`sudo microk8s disable rbac`).

## Preflight (no apply)

From the Personal repo root, review the controller source and pinned base image
in `portfolio/services/openwebui_tools_releaser/`. The image is built locally
 on caos for linux/amd64 and imported into MicroK8s; it is **not** pulled from
 GHCR. The manifest pins the reviewed OCI image manifest digest; after building,
 verify the tag's digest with `ctr images ls`, and create the matching digest
 alias in containerd before starting a Job. A reviewed change to the controller
 requires a new manual build/digest and manifest review; never silently reuse
  `pilot-2` for changed source.

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
ClusterIP `10.152.183.1:443`. There were **two independent blockers**:
Python 3.13 strict X.509 rejected MicroK8s's CA for a missing key-usage
extension; the releaser now relaxes only that check for its mounted Kubernetes
CA (retaining chain and hostname validation). After fixing TLS, a diagnostic
pod with **only** the ClusterIP egress rule again timed out on both named API
GETs, while `192.168.68.6:16443` remained blocked. Calico evaluates the
Service's translated node endpoint in this setup. The narrowly scoped
node-IP:16443 exception is therefore required; all other private and
Tailscale egress remains denied. If the node IP or Service endpoint changes,
stop and review the policy; do not open blanket private egress.
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
    scripts/caos-build.sh portfolio/services/openwebui_tools_releaser Dockerfile openwebui-tools-releaser:pilot-2
    ```

    On `caos`, inspect the newly imported tag in MicroK8s containerd and ensure
    its digest equals the manifest's `sha256:1ffcbe5d…` before creating the
    digest alias (adjust both when the reviewed source changes):

    ```sh
    /snap/microk8s/current/bin/ctr --address /var/snap/microk8s/common/run/containerd.sock -n k8s.io images ls
    /snap/microk8s/current/bin/ctr --address /var/snap/microk8s/common/run/containerd.sock -n k8s.io images tag \
      docker.io/library/openwebui-tools-releaser:pilot-2 \
      docker.io/library/openwebui-tools-releaser@sha256:1ffcbe5d473516e057fda24e78b2883c89b972b46a89b15835ad318d6f5bac37
    ```

3. The initial staged installation was applied with `suspend=true` and kept
   inactive until the pilot and rollback passed. The **approved activation** uses
   this reviewed `suspend=false` manifest: `kubectl apply -f portfolio/k8s/openwebui_tools/releaser.yaml`.
   Confirm the CronJob reports SUSPEND=false and its digest-pinned image exists
   on the target node. Check RBAC:

   ```sh
   kubectl auth can-i get deployments.apps/openwebui-tools -n default --as=system:serviceaccount:default:openwebui-tools-releaser
   kubectl auth can-i patch deployments.apps/openwebui-tools -n default --as=system:serviceaccount:default:openwebui-tools-releaser
   kubectl auth can-i get services/openwebui-tools -n default --as=system:serviceaccount:default:openwebui-tools-releaser
   kubectl auth can-i list deployments.apps -n default --as=system:serviceaccount:default:openwebui-tools-releaser # no
   kubectl auth can-i watch deployments.apps -n default --as=system:serviceaccount:default:openwebui-tools-releaser # no
   kubectl auth can-i get secrets -n default --as=system:serviceaccount:default:openwebui-tools-releaser # no
   ```

4. The one-shot `openwebui-tools-releaser-pilot-3` completed with `rollout_ready`;
   its image digest, `IfNotPresent`, `ClusterIP`, readiness and `/health` were
   verified. A controlled rollback to the previous local image + `Never`, then
   restoration of the GHCR digest + `IfNotPresent`, passed two 1/1 rollouts.
   The attempted-run marker was retained. A Job created manually from a
   suspended CronJob **still runs**; use it only with explicit approval.
5. Recurring jobs run every 10 minutes, forbid concurrent scheduled runs,
   have no retry, and allow 1200 seconds for bounded API calls plus two
   300-second readiness waits. A newer mixed-path push has no eligible build:
   the first scheduled Job after activation should log `no_eligible_build` and
   leave the Deployment unchanged. This was verified by the 13:00 UTC scheduled
   Job on 2026-09-30. A later App-authored, owner-reviewed source-only PR #31
   merged as `f5462283466a9ef8c427d356912f6a4bc87fe177`: the hosted `gate`
   and `build` succeeded (run `36718206313`) and the next scheduled Job
   `openwebui-tools-releaser-29846220` deployed its matching GHCR digest
   `sha256:db60eefdbd12c59f1c5621d5ea3b455377d942ddd6877c7a4044669539f9618a`.
   The Service remained ClusterIP and a real `/crawl` returned one result via
   OpenWebUI. The Deployment was ready 1/1, with `IfNotPresent` and the new
   attempted-run marker. Subsequent scheduled Jobs should log
   `run_already_attempted` while that run remains current.

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

Post-change: check CronJob SUSPEND matches the reviewed desired state, Job status and logs (no tokens or registry
responses), Deployment image/policy and readiness, pods/events for pull failures,
Service `ClusterIP`, RBAC denials, and NetworkPolicy DNS/API/public HTTPS
reachability. Record the deployed digest and next scheduled result; if the Job
fails, leave suspended and investigate instead of widening RBAC or egress.
