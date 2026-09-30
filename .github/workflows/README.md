# openwebui-tools CI / GHCR build pilot

This is a **scaffold, not an activated deployment**. PR checks run on GitHub-hosted
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

### Source-only auto-merge request (explicitly activated)

`openwebui-tools-auto-merge.yml` runs only on GitHub-hosted
`pull_request_target`. It never checks out or executes PR code, and never
approves a PR. It requests `gh pr merge --auto --squash` for a non-draft,
same-repository PR into `main` opened by the dedicated bot login in the repo
variable `OPENWEBUI_TOOLS_BOT_LOGIN`. A fork, missing variable, or any path
outside `services/openwebui_tools/src/` (including a rename **from** outside)
is ineligible. The full paginated file list is checked; 3000 files are rejected
because GitHub may truncate at that limit. The live head SHA is matched at
merge request time. No GH credentials belong in chat or the repository.
GitHub itself rejects `--auto` if repository auto-merge is disabled; the
minimal Actions token need not see that repository setting through REST.
Ineligible author/path PRs end without requesting a merge; API errors and
incomplete file listings fail closed. `OPENWEBUI_TOOLS_BOT_LOGIN` is set to
`home-lab-terminal-app[bot]` and `OPENWEBUI_TOOLS_AUTO_MERGE_READY=true` on the
pilot repository; review and CI checks still gate every merge.

Activation requires a maintainer to enable GitHub auto-merge and set both
repository variables `OPENWEBUI_TOOLS_BOT_LOGIN` to the **exact** dedicated bot
login (for a GitHub App, typically `app-slug[bot]`) and
`OPENWEBUI_TOOLS_AUTO_MERGE_READY=true` only after verifying the following:

- Protect `main` with required `checks` status from the PR workflow and
  required approving reviews; the workflow checks these classic branch
  protection settings and fails closed if it cannot read them. Ruleset-only
  protection is not accepted by this pilot. Keep direct pushes, force pushes
  and bypass permissions restricted; configure CODEOWNERS for protected
  `.github/workflows/**`, `services/openwebui_tools/Dockerfile`, deployment
  manifests and shared build inputs and require code-owner review via branch
  rules. A future workflow/Dockerfile change can affect later trusted builds.
- Have a maintainer create and review the first PRs manually, confirm the
  required check name is exactly `checks` in protection settings, that review
  and CODEOWNERS rules work, and that the default `GITHUB_TOKEN` can read the
  branch-protection rule and request auto-merge. If it cannot read the rule,
  leave this workflow off; do **not** add a broad admin token to work around
  it. The token only requests auto-merge; GitHub still enforces reviews and
  checks. If requiring code-owner review also blocks bot source-only PRs,
  leave these PRs for human review/merge until the rules are safely resolved;
  never weaken workflow/Dockerfile owner protection for this pilot.
- Use a dedicated GitHub App (or other non-`GITHUB_TOKEN` bot identity) to
  author the PR: Actions created with the default `GITHUB_TOKEN` do not
  normally trigger `pull_request_target` workflows. Store App credentials in
  operator-controlled GitHub settings, not in chat, code or this workflow.
  Repository access here is currently READ, so none of these settings or
  protections have been activated by this change.

## Operator activation (manual)

1. Protect `main`: require PR review, the `checks` PR status, CODEOWNERS approval
   for `.github/workflows/**`, the Dockerfile, lockfile, shared build inputs and
   deployment manifests; prohibit direct/force pushes and restrict workflow
   edits. The source-only gate is not a sandbox: a reviewed workflow or base
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
    deploy. No self-hosted runner or GitHub kubeconfig is needed.
    Home MicroK8s now runs `RBAC,Node` (previously `AlwaysAllow`). The releaser
    identity was verified to be denied Secrets, pod exec, lists and other
    Deployments; recheck effective permissions before any manual Job. The
    CronJob remains suspended until a published, reviewed image is tested.

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
