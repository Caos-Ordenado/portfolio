# openwebui-tools CI / caos pilot

This is a **scaffold, not an activated deployment**. PR checks run on GitHub-hosted
`ubuntu-latest` without secrets or cluster access. They compile service/shared
Python sources, syntax-check the existing deploy helper without running it,
and build the real service Dockerfile using only the same tracked build inputs
archived by the deploy job from the PR checkout (including GitHub's merge
checkout when available). The hosted runner starts the image locally and
requires successful `/health` and `/openapi.json` HTTP responses with valid
JSON; the container is removed afterwards. The job has a 20-minute timeout
and never passes `.env` files, host credentials, or repository secrets into the
build context. This is a build/smoke check, not a full integration or security
review. On pushes to `main`, a hosted
gate compares the complete pushed commit range and allows a deploy only when
**every changed path** is under `services/openwebui_tools/src/`. Mixed pushes,
Dockerfile, workflow, deploy helper, Kubernetes manifest, and shared-package
changes do **not** deploy (nor do empty or unknown-base pushes). Changes made
outside the allowlist require a later separately reviewed source-only push to
 deploy. No `workflow_dispatch` or PR-triggered privileged job exists.

### Optional source-only auto-merge request (off by default)

`openwebui-tools-auto-merge.yml` runs only on GitHub-hosted
`pull_request_target`. It never checks out or executes PR code, and never
approves a PR. It requests `gh pr merge --auto --squash` for a non-draft,
same-repository PR into `main` opened by the dedicated bot login in the repo
variable `OPENWEBUI_TOOLS_BOT_LOGIN`. A fork, missing variable, or any path
outside `services/openwebui_tools/src/` (including a rename **from** outside)
is ineligible. The full paginated file list is checked; 3000 files are rejected
because GitHub may truncate at that limit. The live head SHA is matched at
merge request time. No GH credentials belong in chat or the repository.

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

1. Protect `main`: require PR review (including a trusted owner for workflow,
   Dockerfile, shared, and deployment changes), require the `checks` PR status,
   prohibit direct pushes and force pushes, and restrict who can edit Actions
   workflows or administer runners. Review the repository's effective ruleset
   before registering any runner. A merged workflow change can affect later
   trusted pushes; path gating alone is **not** a sandbox.
2. Register a **dedicated repository runner on caos**, with labels
   `self-hosted`, `linux`, `x64`, `caos-openwebui-tools`. Use the official GitHub
   runner installation steps with a short-lived registration token acquired by
   the operator; do not store it in this repository. Restrict runner access to
   this repository, do not share with untrusted repositories, and do not enable
   public-fork PRs on the runner. Run under a dedicated host user with only the
   permissions required for local BuildKit socket, MicroK8s containerd import
   and scoped Kubernetes Deployment updates. Keep runner credentials outside
   the checkout. Host BuildKit and Kubernetes permissions are powerful: treat
   merged source and Docker build steps as trusted code, not as a sandbox.
3. Create the `caos-openwebui-tools` GitHub environment restricted to `main`;
   no environment secrets are needed. Install/verify `buildctl`, local
   `/run/buildkit/buildkitd.sock`, `/snap/microk8s/current/bin/ctr` and
   `kubectl` on caos. Provide the runner a local kubeconfig with only access
   needed to read the existing `openwebui-tools` Service/Deployment and update
   the Deployment image and watch/undo rollouts in namespace `default`; validate
   permissions with `kubectl auth can-i`. Do not grant chat agents or PR jobs
   BuildKit, Kubernetes or runner credentials. The runner needs egress to fetch
   Python dependencies during build. Never place `.env` or tokens in the build
   context; this workflow archives only committed build inputs.
4. Confirm the existing Service is `ClusterIP`, the Deployment uses
   `imagePullPolicy: Never`, the Dockerfile is approved and the runner's local
   MicroK8s containerd is the cluster node where the Pod will land (on a
   multi-node cluster, use a registry or explicit placement before enabling).
   Confirm `git diff --name-only <before> <after>` shows only the allowed source
   paths for the intended test merge. Inspect the Actions gate result before
   trusting a deploy run. No workflow in this pilot commits, pushes or applies
   manifests.

## Rollout and recovery

Merge a reviewed source-only PR to protected `main`. Observe the hosted gate,
then the dedicated caos runner's build/import/set-image/rollout. Images are
tagged `docker.io/library/openwebui-tools:git-<40-character-commit>` and loaded
locally; the checked-in manifest is **not** pinned automatically. Check
`kubectl -n default rollout status deployment/openwebui-tools --timeout=300s`,
`kubectl -n default get deployment openwebui-tools -o wide`,
`kubectl -n default get service openwebui-tools -o jsonpath='{.spec.type}'`
(`ClusterIP`), and from within the cluster query
`http://openwebui-tools.default.svc.cluster.local:8000/health`. Inspect
`kubectl -n default logs -l app=openwebui-tools --tail=100` for errors. No new
Traefik path, public endpoint, or Tailscale entrypoint is created.

If rollout fails, the workflow attempts `kubectl -n default rollout undo
deployment/openwebui-tools`. For manual recovery, pause/disable this workflow
or take the dedicated runner offline to prevent a subsequent push racing the
rollback; run `kubectl -n default rollout history deployment/openwebui-tools`,
then `kubectl -n default rollout undo deployment/openwebui-tools` (or
`--to-revision=<known-good>`), and verify rollout status and in-cluster health.
Retain the previous image in local containerd until recovery is complete.
Because the checked-in manifest retains its old image, a subsequent
`kubectl apply -k k8s/openwebui_tools` can revert the CI image: reconcile this
drift manually in a separately reviewed change before using manifest apply.
If build fails before set-image, no rollback is required. Disable the runner
or remove the workflow to stop the pilot; no external ingress rollback exists.
