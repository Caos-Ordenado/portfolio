# Code-agent trusted image promotion (opt-in, suspended)

This controller reads only the latest successful `code-agent-build.yml` push run
for the **current** main SHA, its `build` job, commit statuses bound to that run,
and the anonymous GHCR tag digest. It fetches both tracked terminal instruction
files through GitHub's contents API at that verified main commit and checks the
decoded UTF-8 bytes against the Git blob SHA. It can patch only the four named
Deployments in `code-agent` (container image, imagePullPolicy, attempted-run
annotation and terminal pod-template restart annotation) and `AGENTS.md` in the
two named terminal ConfigMaps. Terminal Pods restart even when the image digest
is unchanged, so read-only subPath mounts pick up the new instructions. The
original PVC files are not modified.
No arbitrary manifests, RBAC, policies, Secrets, host SSH, kubectl or Docker are
executed by the controller or the terminal. The deployment controller alone has
a Kubernetes token; terminal and broker security contexts stay unchanged.

## Gate / rollout (operator only)

The PR workflow's uniquely named `self-checks` job is an owner-managed required
check; **configure branch protection outside this repository** to require it
alongside the existing `checks`, and restrict workflow/release-control edits to
the owner. The owner chose to give the portfolio App bypass of the separate
review-only ruleset; the protected branch must still require PRs and both checks.
This bypass affects all portfolio PRs by that App, not only self-update PRs,
and the shared App PEM in the autonomous broker also permits requesting private
infra tokens by code convention rather than by credential isolation. The owner
explicitly accepted that cross-repository risk. Leave CronJob suspended until
the live merge and release gates are demonstrated.
The build gate accepts only own-stack Python source/tests and the two tracked
terminal instruction files for a whole push, with no mixed workflow/manifest
changes. Reviewed Dockerfiles and the broker lockfile are archived from the
fixed `TRUSTED_BUILD_BASE` named in both trusted workflows, never from a bot PR;
update that base only through a reviewed workflow change.
It publishes all three immutable images for that exact SHA; the controller
refuses skipped, pending, failed, stale or mismatched runs/statuses. GHCR images
must be publicly anonymously readable. Existing openwebui-tools workflow is
independent and unchanged. CI workflow changes must remain owner-controlled.

A mixed-path main push legitimately skips `verify` and `build`; the controller
reports `no_eligible_build` and completes without touching Deployments or
ConfigMaps. The first suspended one-shot smoke Job on the original controller
image returned `missing_successful_build` on exactly such a push; the corrected
image digest in `self-releaser.yaml` treats a **skipped** build as no release,
while a missing/failed build remains an error. Repeat the smoke after updating
the suspended CronJob; do not unsuspend based on the failed Job.

From `portfolio/`: run `python3 -m pytest -q services/code_agent_self_releaser/tests`,
`actionlint .github/workflows/code-agent-{pr,build}.yml`,
`kubectl kustomize k8s/code-agent` (the new overlay must NOT appear), and
`kubectl apply --dry-run=client -f k8s/code-agent/self-releaser.yaml`.
Verify home MicroK8s context, policy enforcement, CoreDNS labels, Service DNAT
and deployment/container names. Verify the reviewed controller image imported
on caos matches the digest in `k8s/code-agent/self-releaser.yaml`; build changes
from `services/code_agent_self_releaser/` only as an operator. Before applying this standalone
overlay. Do not apply the entire code-agent tree. `kubectl apply -f
k8s/code-agent/self-releaser.yaml` creates a **suspended** CronJob. Check
`kubectl -n code-agent get cronjob code-agent-self-releaser -o yaml` and
`kubectl -n code-agent auth can-i patch deployments/code-agent-pr-broker
--as=system:serviceaccount:code-agent:code-agent-self-releaser` plus a denial
for `get secrets` and other Deployment names. Run one manually approved
one-shot Job from the pinned CronJob, inspect logs and all four rollouts,
then only after rollback drill and owner confirmation unsuspend:
`kubectl -n code-agent patch cronjob code-agent-self-releaser -p '{"spec":{"suspend":false}}'`.
The cluster's `code-agent` namespace must already exist. Do not change any
Deployment replica count or auth/Secret wiring through this mechanism.

Kubernetes NetworkPolicy has no GitHub/GHCR FQDN filtering: this policy allows
public IPv4 TCP 443, DNS and the home MicroK8s API IP/port only. The Python
client uses fixed HTTPS API/GHCR URLs with TLS, disables proxies and redirects.
If strict domain-only network egress is required, block release until a
reviewed FQDN-capable egress layer exists. No ingress/Traefik/Tailscale route.

## Rollback / post-change

First suspend `kubectl -n code-agent patch cronjob code-agent-self-releaser
-p '{"spec":{"suspend":true}}'`; wait for any running Job to finish or
stop it deliberately after inspecting state. The controller records the
previous image/policy, terminal restart annotation and ConfigMap content in
memory; it restores patched Deployments and ConfigMaps in reverse order on failed
readiness, then fails the Job. Concurrent changes are not overwritten. If the
Job is interrupted or restore fails, inspect the two ConfigMaps' `AGENTS.md`
values against the reviewed instruction files at the intended commit and restore
their previous operator-recorded contents (do not paste private material into
logs). Restore the four previously recorded digest/policy pairs and terminal
pod-template restart annotations from operator rollout notes or Deployment
history (`kubectl -n code-agent rollout history deployment/NAME`). Restart both
terminal Deployments after restoring ConfigMaps so subPath mounts refresh.
Do **not** use a stale local `Never` image unless the digest is still present.
Verify `kubectl -n code-agent rollout status deployment/NAME --timeout=300s`
for each of the four (zero terminal replicas block instruction promotion), and inspect
`kubectl -n code-agent get deployments,cronjobs,jobs,pods,networkpolicies` and
`kubectl -n code-agent logs job/JOB_NAME` without exposing tokens. Confirm
broker health/internal-only ingress, diagnostics authorization, terminal
admin-only WebUI integration, denied private/Tailscale egress, no changed
Secret mounts, and no new public service. If terminal integration is unsafe,
disable it in WebUI before troubleshooting. To retire the controller,
delete only `k8s/code-agent/self-releaser.yaml`, not the code-agent overlay.

Bootstrap the two named ConfigMaps with reviewed instructions before enabling the
CronJob, from the `portfolio/` root:

```sh
kubectl -n code-agent create configmap open-terminal-agents \
  --from-file=AGENTS.md=k8s/code-agent/terminal-AGENTS.md
kubectl -n code-agent create configmap infra-terminal-agents \
  --from-file=AGENTS.md=k8s/code-agent/infra-review/infra-terminal-AGENTS.md
```

Stop if either already exists; compare contents without overwriting operator
work. The controller never creates missing ConfigMaps or patches PVCs. After
both exist, apply only the two reviewed terminal Deployments, retaining the
current live `infra-terminal` replica count (the tracked manifest defaults to
zero); confirm their read-only `/home/user/AGENTS.md` subPath mounts show the
reviewed instructions while PVC files remain unchanged. The controller refuses
instruction promotion if either terminal has zero desired replicas. Never
auto-apply arbitrary repository manifests or let the bot edit this workflow,
RBAC or releaser overlay.
