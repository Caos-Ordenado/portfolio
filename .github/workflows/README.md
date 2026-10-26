# Portfolio hosted CI / GHCR trusted builds

## Code-agent self-update lane (live)

Portfolio `main` requires PRs with strict `checks` and `self-checks` from
GitHub Actions App 15368, admin enforced. The review-only portfolio ruleset
allows the owner and Integration App 5113000 to bypass review. **This applies
to all portfolio App PRs**, not just `agent/self-update/`; never promise manual
review as a merge gate for App-authored portfolio changes. Private `infra`
remains review-only with owner-only bypass. Bot self-update PRs are limited by
the broker's own-stack full-path allowlist; its poller verifies the unchanged
head and current base, two completed successful head-bound Actions checks, then
uses App REST squash merge if GitHub leaves queued auto-merge `REVIEW_REQUIRED`.
An actual App PR #53 on tracked terminal instructions merged without reviews
after both checks. Hosted run `40003940789` gate/verify/build passed; three
anonymous GHCR digests matched the run-bound commit statuses. The supervised
controller promoted main `87d3…` and reported `rollout_ready`; four Deployments
were 1/1 with both mounted instruction ConfigMaps matching main. Verify live
state rather than treating a successful hosted build as proof of deployment.

The build gate accepts only own-stack Python source/tests and tracked terminal
instructions on a whole main push. Dockerfiles, RBAC, network, workflows,
controller manifests and private infra remain owner-managed trust roots.
The workflows archive Dockerfiles and the broker lockfile from their fixed
reviewed `TRUSTED_BUILD_BASE`, not an App PR. Changes outside the build gate
can skip `verify` and `build`; `no_eligible_build` is a successful no-op, not
a failed rollout. The separate `code-agent-self-releaser` CronJob is live every
10 minutes in `code-agent` with a locally imported pinned image; the checked-in
`k8s/code-agent/self-releaser.yaml` still has `suspend: true` and must not be
reapplied without reconciling live state. `run_already_attempted` is another
successful no-op. For real rollout and recovery checks see
`services/code_agent_self_releaser/README.md`; suspend that CronJob first on
release failure, and revoke the App portfolio review bypass if disabling
autonomous merges. The accepted shared App PEM can technically request private
infra tokens; code-level repository scoping is not credential isolation.

## openwebui-tools lane

PR checks run on GitHub-hosted
`ubuntu-latest` without secrets or cluster access. They compile service/shared
Python sources, syntax-check the existing deploy helper without running it,
and build the real service Dockerfile using only the same tracked build inputs
archived by the build job from the PR checkout (including GitHub's merge
checkout when available). The hosted runner starts the image locally and
requires successful `/health` and `/openapi.json` HTTP responses with valid
JSON; the container is removed afterwards. The job has a 20-minute timeout
and never passes `.env` files, host credentials, or repository secrets into the
build context. This is a build/smoke check, not a full integration or security
review. On pushes to `main`, a hosted
gate compares the complete pushed commit range with `git diff --no-renames` and
allows a build/publish only when
**every changed path** is under `services/openwebui_tools/src/`. Mixed pushes,
Dockerfile, workflow, deploy helper, Kubernetes manifest, and shared-package
changes do **not** publish (nor do empty or unknown-base pushes). Changes made
outside the allowlist require a later separately reviewed source-only push to
publish. No `workflow_dispatch`, runner, kubeconfig, Tailscale connection, or
cluster deployment job exists in this workflow. The stable workflow name is
`openwebui-tools trusted deploy` and the publish job ID is `build` for the
separate in-cluster controller's GitHub run polling.

### Source-only auto-merge request

The private PR broker (`services/code_agent_pr_broker/`) uses the scoped
`home-lab-terminal-app` installation token to create source-only PRs and request
squash auto-merge. Protected `main` requires `checks` and `self-checks` for every
PR. The portfolio review-only GitHub ruleset permits the owner **and App** to
bypass review; legacy App source-only PRs no longer have a guaranteed owner
review gate. The App
key stays in the broker,
not in chat, the terminal, or GitHub Actions. The previous
`pull_request_target` request workflow was removed: GitHub denies its minimal
`GITHUB_TOKEN` the `enablePullRequestAutoMerge` mutation, and a merge initiated
with `GITHUB_TOKEN` would not reliably trigger the downstream push workflow.

## Trust and release configuration

1. Protect `main`: require PRs and strict `checks` and `self-checks` from Actions
   App 15368 for every actor, including admins. The separate portfolio review-only
   ruleset requires review and CODEOWNER approval but currently allows the owner
   and Integration App 5113000 to bypass it. Private infra retains owner-only
   bypass. Retain CODEOWNERS, prohibit
   direct/force pushes and restrict workflow edits. The source-only gate is not
   a sandbox: a reviewed workflow or base
   image change can affect later trusted builds. Keep the Dockerfile base image
   digest pinned and runtime dependencies hashed in `requirements.lock`.
2. Allow the repository's `GITHUB_TOKEN` to publish to
   `ghcr.io/caos-ordenado/openwebui-tools`. Only the hosted `build` job receives
   `packages:write` and `statuses:write`; `gate` has `contents:read` only. Set
   the GHCR package to **public** in package settings (new packages can default
   to private). The
   job archives committed Dockerfile, lockfile, source and shared inputs,
   builds `linux/amd64`, pushes `git-<40-character-main-SHA>` and records the
   resulting `sha256:` digest. It checks anonymous `docker manifest inspect`
   with an empty Docker config; a raw unauthenticated `curl` can receive a 401
   challenge even when the image is public. After that check, it POSTs a commit
   status via `gh api` on the pushed SHA: context `openwebui-tools-image`, state
   `success`, description exactly the published `sha256:<64>` digest, target URL
   `https://github.com/Caos-Ordenado/portfolio/actions/runs/<run-id>`. A failed
   status POST fails `build`; a private package fails before the POST. Never
   place tokens, kubeconfig, or `.env` in the archive or logs.
3. Before enabling any rollout, separately review and activate the **in-cluster**
   CronJob controller (not defined by this workflow). It must poll GitHub
   workflow runs for this exact workflow on `main`, require a successful `build`
   job for the **current main SHA** (not merely a successful gate or PR), and
   require the matching `openwebui-tools-image` success status for that SHA with
   its description equal to the public GHCR digest for `git-<SHA>` and its
   target URL equal to the successful build run. GitHub commit statuses are
   appendable later by other authorized writers: a status alone is not proof
   of a successful trusted build. Restrict branch ownership, workflow edits,
   and status-writing permissions; reject stale/out-of-order or repeat runs,
   and pin the Deployment to the verified digest rather than a mutable tag.
   Grant only the necessary read access to GitHub and narrowly scoped
   Kubernetes permissions; keep credentials in cluster Secrets, not ConfigMaps,
    GitHub secrets, or repo files. Review concurrency and safe rollback of both
    image and pull policy. Without that controller, publishing does **not**
    deploy. No self-hosted runner or GitHub kubeconfig is needed. Home MicroK8s
    runs `RBAC,Node`; the releaser identity was verified to be denied Secrets,
    pod exec, lists and other Deployments. Recheck after RBAC changes. The
    one-shot rollout and manual rollback drill passed; the reviewed
    `k8s/openwebui_tools/releaser.yaml` enables recurrence after separate owner
    approval. Check its effective RBAC and every scheduled Job.

### Review-rule operational boundary

The portfolio App bypass is already live; a checked App PR may merge without
owner review. It does not remove protected-branch PR/strict check enforcement.
Do not copy this exception to private infra. If autonomous portfolio merges
must stop, revoke the App's portfolio review-only ruleset bypass and disable
the broker poller/callers with operator authorization; suspending the controller
alone prevents deployment, not merges. Verify settings and a fresh check result
before restoring autonomous operation.

## Rollout and recovery

Merge a reviewed source-only PR to protected `main` after the public package is
configured. Confirm the `gate` and `build` job statuses, the `git-<SHA>` tag,
the published digest, anonymous manifest inspection and matching
`openwebui-tools-image` commit status (description digest and target run URL).
Only a successful build for the current `main` SHA is eligible; no stale or
repeat release. Do not turn on the separate in-cluster controller until its
authorization, SHA/digest verification,
rollout and rollback have been reviewed. The existing Service must remain
`ClusterIP`; no new Traefik route, public application endpoint or Tailscale
entrypoint is created. Verify nodes can pull the public `linux/amd64` digest.
The checked-in manifest still uses a local dev image and `Never`: do **not**
re-apply it over a controller-managed rollout without reconciling image drift
in a separately reviewed change.

To stop publishing, disable the workflow or block merges to `main`; to stop
rollouts, suspend the in-cluster CronJob first. Failed builds do not need a
cluster rollback. If a controller rollout fails, inspect
`kubectl -n default rollout history deployment/openwebui-tools` and restore the
recorded known-good image **and pull policy** with operator credentials (or use
`kubectl -n default rollout undo deployment/openwebui-tools --to-revision=<known-good>`
after checking that revision restores both). Keep the previous digest available
through recovery. Confirm `kubectl -n default rollout status
deployment/openwebui-tools --timeout=300s`,
`kubectl -n default get deployment openwebui-tools -o wide`,
`kubectl -n default get service openwebui-tools -o jsonpath='{.spec.type}'`
(`ClusterIP`), in-cluster
`http://openwebui-tools.default.svc.cluster.local:8000/health`, and
`kubectl -n default logs -l app=openwebui-tools --tail=100` for errors.
