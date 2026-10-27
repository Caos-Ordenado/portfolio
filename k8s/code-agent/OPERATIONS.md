# OpenWebUI coding agents: end-to-end operations

This is the entry point for future sessions. `portfolio` is public; `infra` is
private. The two Open Terminal connections share the home MicroK8s `code-agent`
namespace but have separate PVCs, API keys, brokers and network policies.
Check live GitHub and Kubernetes state before following a historical example:
the checked-in manifests are **not** a live-state reconciler.

| Agent | Workspace | PR destination | Merge and release |
| --- | --- | --- | --- |
| Public `open-terminal` | HTTPS checkout `/home/user/portfolio` | Public `Caos-Ordenado/portfolio` through `code-agent-pr-broker:8001` | Eligible own-stack PRs merge after required checks, without owner review. Hosted build and scoped CronJobs release eligible code. |
| Private `infra-terminal` | Audited subset `/home/user/infra`, **not** a Git checkout | Private `Caos-Ordenado/infra` through `infra-pr-broker:8001` | `checks` **and owner review** gate merge; an operator deploys approved infra changes. No autonomous infra release. |

Both Open Terminal services are ClusterIP-only on port 8000, proxied by their
separate owner-configured **system** connections in public Open WebUI. Neither
terminal has a Kubernetes ServiceAccount token, Docker socket, host SSH, GitHub
App PEM or direct push credentials. The private terminal has no DNS or public
HTTPS egress; the public terminal retains public HTTPS egress. Do not put private
infra source in a portfolio PR.

## GitHub identity and repository rules

The installed `gh` binary does **not** support `--profile`. Other sessions may
change its active account: never use `gh auth switch` here. For operator calls,
select `Caos-Ordenado` **per process** without printing or persisting its token:

```sh
set +x
gh_caos() {
  local token login
  token="$(gh auth token --user Caos-Ordenado)" || return 1
  login="$(GH_TOKEN="$token" gh api user --jq .login)" || return 1
  test "$login" = Caos-Ordenado || return 1
  GH_TOKEN="$token" gh "$@"
}
gh_caos pr list -R Caos-Ordenado/portfolio --state open
gh_caos pr list -R Caos-Ordenado/infra --state open
```

Pass `-R Caos-Ordenado/portfolio` or `-R Caos-Ordenado/infra` to PR commands;
`gh repo view` takes the full repository as a positional argument instead.
For `gh api`, put the full `repos/Caos-Ordenado/REPO/...` path in the call.

Both `main` branches require a PR, strict `checks`, resolved conversations and
admin enforcement. Public `portfolio/main` **also** requires `self-checks`
from GitHub Actions App ID 15368. The separate review-only portfolio ruleset
allows the owner and Home Lab Terminal App (App ID 5113000) to bypass review;
this affects **all** App-authored portfolio PRs, not just the self-update lane.
Private `infra/main` requires CODEOWNER approval through its review-only
ruleset: only `Caos-Ordenado` can bypass it, **not the App**. See
`portfolio/.github/workflows/README.md` and private `infra/.github/workflows/README.md`.

The App installation covers exactly these two repositories. Each current broker
requests a token scoped to its selected repository; **the App PEM itself is
shared**. Autonomous public broker code technically could request private
`infra` access. The owner explicitly accepted this cross-repository risk;
repo-scoped requests in today's code are not credential isolation.

## Public self-update: from task to running image

1. The public terminal works in a clean public checkout and reads its effective
   `/home/user/AGENTS.md` from the read-only `open-terminal-agents` ConfigMap
   subPath mount. It edits the **tracked** source in its checkout, not the
   mounted file. Its self-update lane permits only `.py` under
   `services/openwebui_tools/src/`, `services/code_agent_pr_broker/broker/`,
   `services/cluster_diagnostics/diagnostics/` and their corresponding `tests/`,
   plus the two exact tracked terminal instruction files under
   `k8s/code-agent/`. The terminal's direct Kubernetes access remains denied.
   Dockerfiles, dependency locks, `.github/`, RBAC, NetworkPolicies, release
   controller, Secrets and deployment manifests are operator-managed trust
   roots. An unrelated fix such as Dozzle is **not** a self-update path.
   A tests-only PR under the **broker or diagnostics** `tests/` is eligible for
   the code-agent build and can still trigger a release. A PR changing only
   `services/openwebui_tools/tests/` is broker-allowlisted but neither the
   code-agent build nor the separate tools source-only build deploys it.
2. After local checks, it sends full UTF-8 file contents, title/body/message and
   `expected_base_sha` (clean checkout `git rev-parse HEAD`) to
   `POST http://code-agent-pr-broker.code-agent.svc.cluster.local:8001/self-proposals`.
   The broker accepts at most 20 files/64 KiB. It rejects stale main (409),
   unauthorized paths (422), symlinks, no-op changes and caller-controlled
   repository/ref/URL. The PR appears **publicly immediately** on a random
   `agent/self-update/` branch. Do not submit private infra material or secrets.
   The legacy `/proposals` source-only lane and wider `/review-proposals` lane
   are different: the latter never requests auto-merge.
3. Hosted PR workflows run `checks` and `self-checks` on the proposed head.
   The broker polls open App-authored self PRs, rechecks the complete bounded
   changed-file list (including rename origins), unchanged head/base and live
   main, then requires **both** completed successful head-bound check runs from
   GitHub Actions App 15368. GitHub can leave its auto-merge request queued at
   `REVIEW_REQUIRED` even with the App review bypass. The broker then makes a
   repo-scoped App REST squash-merge request bound to the exact head SHA;
   protected PR/check requirements are still enforced. `auto_merge_queued`
   means only that the initial request was queued, **not** that a merge or
   deployment occurred. PR #53 verified no-review merge after both checks.
4. On a **whole push** changing only eligible own-stack Python source/tests or
   tracked instructions, `.github/workflows/code-agent-build.yml` runs `gate`,
   unprivileged `verify`, and a separate privileged `build` runner. Trusted
   Dockerfiles and the broker lockfile come from the fixed reviewed
   `TRUSTED_BUILD_BASE` in the workflows, never from an agent PR. The build
   publishes immutable `git-<main SHA>` GHCR tags and run-bound digest statuses
   for broker, diagnostics and hardened terminal, verifying anonymous pulls.
   It builds all three even for broker/diagnostics tests-only or terminal
   instruction-only eligible pushes;
   the controller may restart all four named Deployments for that release.
   A mixed-path or docs-only main push deliberately **skips** image publication.
   Independently, a source-only `services/openwebui_tools/src/` push is eligible
   for its existing hosted build and the `default/openwebui-tools-releaser`.
5. `code-agent/code-agent-self-releaser` is an operator-installed CronJob every
   ten minutes. It requires a successful build **for current main**, matching
   run-bound statuses and anonymous registry digests. Its ServiceAccount may
   GET/PATCH only the four named code-agent Deployments (`code-agent-pr-broker`,
   `cluster-diagnostics`, `open-terminal`, `infra-terminal`) and the two named
   terminal instruction ConfigMaps; it cannot access Secrets or apply arbitrary
   manifests. It records prior images/pull policies and instructions, updates
   digests, restarts terminals so their read-only subPath mounts refreshes, waits
   for 1/1, and attempts rollback on failed readiness. It does **not** deploy
   private infra workloads. A terminated Job or failed restore needs operator
   recovery. See `services/code_agent_self_releaser/README.md`.

**Observed:** App PR #53 merged without reviews after both checks; trusted build
run `40003940789` passed, the supervised one-shot reported `rollout_ready`,
four workloads and both mounted instructions were verified, and the manual
cluster-diagnostics image/pull-policy rollback and restore worked. The CronJob
is live; a genuinely scheduled Job on 2026-10-26 at 13:20 UTC returned
`run_already_attempted`. Later mixed-path main pushes yield
`no_eligible_build`. Two earlier scheduled failures (`network_failure`,
`http_403`) were followed by successes; rate limiting was **not** established
as their cause.

## Private infra review: from audited snapshot to PR

1. From a **clean separate `infra/main` checkout** equal to `origin/main`, the
   operator audits the tracked files selected by
   `portfolio/k8s/code-agent/infra-review/create-snapshot.py`. It exports a
   filtered tar and NUL-delimited Git blob manifest into a new mode-0700
   directory **outside both repos**. The output parent must already exist with
   mode 0700. No `.git`, ignored/dirty files, `.env`, PEM,
   certificates, symlinks, deploy scripts or full-tree `git archive` go to the
   terminal. The exporter is not a substitute for operator content review.
   Set `INFRA_REPO` to the audited **clean `infra/main` worktree** (never the
   existing feature checkout). In the `Personal/` workspace, from `portfolio/`:

   ```sh
   python3 k8s/code-agent/infra-review/create-snapshot.py "$INFRA_REPO" \
     "$HOME/.local/share/openwebui-agent/infra-snapshots"
   ```

2. The operator checks the tar SHA-256 and copies **only** that tar and manifest
   to the separate `infra-terminal-home` PVC, extracts into a fresh empty
   `/home/user/infra`, and runs
   `portfolio/k8s/code-agent/infra-review/verify-snapshot.py` against the
   extracted tree. Both terminals receive only their own Open Terminal API key;
   the private terminal does **not** receive the App PEM. The operator creates
   `infra-review-snapshot` ConfigMap key `MAIN_SHA` outside git. The private
   broker reads it as `REVIEW_SNAPSHOT_SHA`; a read-only SHA marker is projected
   into the terminal. The marker is not proof of PVC contents. Full operator
   staging, refresh and rollback commands are in `infra-review/README.md`.
   For a refresh of a running private terminal, first disable its WebUI
   connection and suspend the public self-releaser; wait for any active release
   Job so it cannot restart the terminal during replacement. Restore its
   previous suspension state only after the new snapshot is verified.
3. The private terminal sends allowed full-file changes to
   `POST http://$INFRA_PR_BROKER_SERVICE_HOST:8001/review-proposals` with
   `expected_base_sha` equal to the audited marker. The infra-mode broker
   rejects legacy/public/self endpoints, verifies snapshot SHA **and** live
   private `infra/main`, and returns 409 without writing when either is stale.
   Its installation token is restricted to `infra`. The resulting App PR stays
   **inside the private repo**, never auto-merges, and cannot deploy anything.
4. `infra/.github/workflows/infra-pr.yml` runs the required `checks` (tracked
   shell syntax, `kubectl kustomize k8s`, and builds changed Nuxt sites). The
   private ruleset requires owner CODEOWNER review for an App PR. Only after
   approval and checks can the owner merge; site/hosting/Kubernetes changes are
   deployed manually with their existing reviewed procedures. The portfolio
   self-releaser may update the **terminal image/instructions** but never infra
   repo workloads.

**As checked on 2026-10-26, the mounted private snapshot is older than
`infra/main`.** Private proposals should fail with 409 until the operator
refreshes/audits the snapshot and marker by the private runbook. The local
`infra/` worktree was on an unrelated feature branch at that check; preserve it
and use a clean main worktree rather than switching or overwriting user work.
No private commit SHA or snapshot contents belong in this public document.

## Cluster visibility and operator checks

`cluster-diagnostics.code-agent.svc.cluster.local:8002` is ClusterIP-only and
accepts separate per-terminal bearer keys from a Secret. Only that service
holds a scoped Kubernetes ServiceAccount: resource **summaries** and pod logs
across namespaces, no Secret/ConfigMap objects, pod exec or writes. `/kinds`,
`/resources?kind=pods&namespace=observability` (paginated) and
`/logs?namespace=observability&pod=NAME&tail=100` are read-only. Logs are
**unredacted** but capped per request at 300 lines/64 KiB; when the owner asks
for logs in chat, return requested lines verbatim. The owner explicitly
accepted model-context exposure and the possibility that the portfolio
terminal's existing public HTTPS/PR path could disclose log-derived data.
Never print bearer tokens. The infra terminal has no public HTTPS or DNS.
See `diagnostics/README.md` and `services/cluster_diagnostics/README.md`.

Run these on an **operator workstation** from `portfolio/`, not in a terminal:

```sh
kubectl config current-context # must be home microk8s
kubectl -n code-agent get deployments code-agent-pr-broker infra-pr-broker open-terminal infra-terminal cluster-diagnostics
kubectl -n code-agent get svc,endpoints,networkpolicy
kubectl -n code-agent get cronjob code-agent-self-releaser
kubectl -n code-agent get jobs -l app=code-agent-self-releaser --sort-by=.metadata.creationTimestamp
kubectl -n default get cronjob openwebui-tools-releaser
```

For a scheduled Job, read its own sanitized controller log using
`kubectl -n code-agent logs job/JOB_NAME`. `rollout_ready` means check the four
Deployments **and** mounted instructions; `run_already_attempted` and
`no_eligible_build` are successful no-ops. Repeated `network_failure` or
`http_403`, a pending/failed hosted build, or a different current-main SHA
requires investigation; do not promote an older digest. Check real Pod health,
Service endpoints and WebUI terminal authentication rather than TCP only.

## Stop, rollback and drift

1. **Stop autonomous deployment:** suspend the live CronJob with
   `kubectl -n code-agent patch cronjob code-agent-self-releaser --type=merge -p '{"spec":{"suspend":true}}'`.
   A running Job must be inspected separately; suspension does not cancel it.
   To stop future **merges** as well, remove only the App's bypass actor from
   portfolio's review-only GitHub ruleset (owner bypass and required checks stay).
   Suspending the CronJob alone does not stop broker PR publication or merging.
2. For a failed image rollout, restore the **recorded prior image digest and
   `imagePullPolicy` together** for each affected named Deployment, then wait
   for readiness. Restore prior instruction ConfigMap contents **before**
   restarting the two terminals; subPath mounts refresh only on Pod recreation.
   Preserve both PVCs and the private snapshot. For a private-source incident,
   disable/remove the infra WebUI connection first, then scale only
   `infra-terminal`/`infra-pr-broker` to zero; use
   `infra-review/README.md` for the exact sequence.
3. **Source-of-truth drift is intentional:** checked-in
   `k8s/code-agent/self-releaser.yaml` has `suspend: true`, while the live
   CronJob runs; the diagnostics and private-infra overlays default to zero
   replicas while live Pods are 1/1. Old broker/terminal Deployment manifests
   may pin bootstrap/local images and `Never`, while the scoped releaser manages
   newer GHCR digests with `IfNotPresent`. Do **not** blindly run
   `kubectl apply -k k8s/code-agent`, `kubectl apply -k k8s/code-agent/infra-review`,
   or reapply stale standalone manifests over live workloads. Compare rendered
   diff, current image **and pull policy**, replica count, ConfigMaps and
   controller state before any manual apply. Never delete the entire namespace
   or PVC as a shortcut.

Targeted local checks from `portfolio/` before a reviewed operator change:

```sh
PYTHONPATH=services/code_agent_pr_broker:services/cluster_diagnostics python3 -m pytest -q services/code_agent_pr_broker/tests services/cluster_diagnostics/tests
python3 -m pytest -q services/code_agent_self_releaser/tests
actionlint .github/workflows/code-agent-pr.yml .github/workflows/code-agent-build.yml
kubectl kustomize k8s/code-agent
kubectl apply --dry-run=server -f k8s/code-agent/self-releaser.yaml
```

Run `python3 -m pytest -q tests/test_broker.py` from
`portfolio/services/code_agent_pr_broker/` if you prefer a single broker test.
Do not run release or
snapshot commands against an unverified Kubernetes context or a dirty/private
checkout.
