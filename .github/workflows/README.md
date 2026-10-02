# openwebui-tools CI / GHCR build pilot

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
squash auto-merge. Protected `main` requires `checks` for every PR. A separate
review-only GitHub ruleset requires an owner review for App-authored PRs, while
`Caos-Ordenado` may bypass that ruleset when merging their own PRs. The App
key stays in the broker,
not in chat, the terminal, or GitHub Actions. The previous
`pull_request_target` request workflow was removed: GitHub denies its minimal
`GITHUB_TOKEN` the `enablePullRequestAutoMerge` mutation, and a merge initiated
with `GITHUB_TOKEN` would not reliably trigger the downstream push workflow.

## Trust and release configuration

1. Protect `main`: require PRs and `checks` in branch protection for every
   actor, including admins. In a separate review-only ruleset targeting `main`,
   require one review and CODEOWNER approval, with only `Caos-Ordenado` (user ID
   130296975) permitted to bypass the ruleset on pull requests. The GitHub App
   must not bypass it. This lets the owner merge their own PR after `checks`
   while preserving the App PR review gate. Retain CODEOWNERS, prohibit
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
    The one-shot rollout and manual rollback drill passed; the reviewed
    `k8s/openwebui_tools/releaser.yaml` enables recurrence after separate owner
    approval. Check its effective RBAC and every scheduled Job.

### Review-rule migration

Create and verify the review-only ruleset **before** lowering GitHub's global
approval count to zero. The ruleset bypass is `pull_request` for user
`Caos-Ordenado` only; it does not bypass branch protection's `checks`, PR-only
merge, admin enforcement or conversation resolution. After the ruleset is
active, lower only the redundant branch-protection approval count to zero
and disable its CODEOWNER requirement (retaining PR-only merge). Confirm a checked
owner-authored PR merges without a second reviewer, and an unapproved App PR
remains blocked until the owner approves its current head. If the ruleset
does not enforce that boundary, restore the branch-protection review requirement and
leave the new broker inactive pending correction.

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
