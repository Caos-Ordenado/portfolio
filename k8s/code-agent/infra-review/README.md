# Phase 2: private infra review (staged, not connected to WebUI)

This is a **standalone** overlay for the existing `code-agent` namespace, absent
from `../kustomization.yaml` and the portfolio root. Both Deployments default to
**zero replicas** in git. After reviewed PR #35 merged, the overlay was applied
to home MicroK8s and the two private workloads were manually scaled to one for
isolated testing. The pinned pilot-6 image was verified on caos; repository-scoped
authorization, denied cross-broker ingress, terminal DNS/public egress, and the
curated snapshot's 45 Git blobs were tested. No new Open WebUI connection was
created in this rollout. Do not enable one before effective admin-only access,
retention and model data handling have been checked in the running WebUI.
No new Kubernetes deployer, public
auto-deploy, direct git push, Ingress, IngressRoute, Traefik prefix, NodePort or
LoadBalancer is included. Existing portfolio terminal/broker stay unchanged.

`infra-terminal.code-agent.svc.cluster.local:8000` is ClusterIP-only, with
ingress from `default`/`app=openwebui` on 8000 and egress **only** to
in-namespace `app=infra-pr-broker` pods on TCP 8001. It has **no DNS egress**,
public HTTPS egress (including GitHub), or other IP egress. Create the broker
Service before creating the terminal Pod so Kubernetes injects
`INFRA_PR_BROKER_SERVICE_HOST`; the tracked `infra-terminal-AGENTS.md` uses
`http://$INFRA_PR_BROKER_SERVICE_HOST:8001` without DNS. Check Service-IP
translation against the pod-selector policy on home Calico. If it fails, stop;
do not add DNS or broad IP egress. `infra-pr-broker.code-agent.svc.cluster.local:8001` is ClusterIP-only:
ingress only from the infra terminal, egress to CoreDNS and public IPv4 TCP 443
with private, Tailscale CGNAT and reserved ranges excluded. NetworkPolicy
cannot enforce GitHub DNS names; public HTTPS is not GitHub-only. Confirm actual
Pod/Service CIDRs and dual-stack CNI behavior before any rollout. Existing
NetworkPolicies are additive: audit other policies in this namespace for
selectors overlapping these new pods; an additional allow rule can defeat
isolation. No Kubernetes service account token in either pod.

The terminal has its **own** RWO PVC (`infra-terminal-home`) and **own** Secret
(`infra-terminal-api-key`, key `OPEN_TERMINAL_API_KEY`). It does not receive the
GitHub App PEM or IDs. The broker references existing `code-agent-github-app`
keys `APP_ID`, `INSTALLATION_ID`, and `private-key.pem`, mounted read-only; no
Secret value is stored in this repo or a ConfigMap. Never grant the terminal
Kubernetes API access. The hardened terminal image is built from `../Dockerfile`
and pinned to its verified locally imported digest with `imagePullPolicy: Never`;
the broker image is pinned to the locally imported pilot-6 review digest.

## Access blockers / contract before connecting WebUI

- Review and verify the broker's staged `REVIEW_REPOSITORY=infra` fail-closed mode;
  reject unsupported values, verify submitted base commit against the operator's
  expected snapshot SHA, confine proposals to the intended private repository,
  prevent public PRs and deployment, and reject portfolio requests. Verify the
  imported image digest matches this manifest **before** increasing
  replicas. Review/write mode must remain subject to human approval; do not
  attach a Kubernetes deployer. The snapshot-SHA handoff is specified here but
  must be implemented and verified in the new broker image; do not activate on
  the strength of an archive checksum alone. The broker reads
  `REVIEW_SNAPSHOT_SHA` from operator-created `infra-review-snapshot` ConfigMap
  key `MAIN_SHA`; the terminal sees a separate read-only projected SHA marker
  at `/run/review-snapshot/MAIN_SHA`. The marker is not a trust anchor for PVC
  contents; the operator must verify the extracted files against git blobs.
- The existing GitHub App installation is installed on **exactly portfolio and
  infra**. Verify this still holds and that each broker requests a token scoped
  to its own repository (`infra` for review, `portfolio` for public proposals),
   with minimum required permissions. Do not issue an installation-wide token.
   If a separate App credential is required, revise Secret references and
   rotation plan first. Phase 2 broker source and image are staged; its
   deployments remain inactive until the reviewed PR is merged and rollout
   prerequisites are met.
- Require explicit owner approval for private code in a public-facing Open
  WebUI process: prompts, responses, terminal output, broker logs, models and
  PVC backups can leak source. Confirm WebUI's effective admin-only connection
  grants, retention and model data handling. Private code must not be published
  by a portfolio PR or copied into chat or logs. No `.env`, cert, PEM, `.git`,
  untracked files or local working-tree changes belong in the snapshot.
- Obtain release/security review, home-context/CNI/StorageClass checks and an
  approved rollback before activation. TCP readiness alone does not verify
  isolation, repo authorization or review-only operation.

## Staged operator procedure (only AFTER blockers are resolved)

1. From `portfolio/`, check `kubectl config current-context` targets **home
   MicroK8s**, `kubectl get pods -n kube-system --show-labels` matches CoreDNS,
   `kubectl get pods -n default -l app=openwebui --show-labels` matches ingress,
   `kubectl -n code-agent get networkpolicy`, and the namespace and storage
   provisioner exist. Inspect rendered resources with
   `kubectl kustomize k8s/code-agent/infra-review` and optionally, on the
   confirmed home context, `kubectl apply --dry-run=client -k k8s/code-agent/infra-review`.
   Do not apply the parent overlay, which manages the portfolio pilot.
 2. From the Mac, obtain approval to snapshot **private infra main**. Check
   `git -C ../infra status --porcelain=v1 --untracked-files=all` is **empty**,
   `git -C ../infra branch --show-current` is `main`, and HEAD is the audited
    main commit (the exporter compares HEAD with `origin/main`; verify remote
    provenance separately). Audit the **allowlisted tracked contents** for
    credentials or material unsafe for the model, including tokens embedded in
    otherwise safe-looking Markdown, YAML or source. The exporter rejects
    non-regular Git entries, dotfiles, deployment scripts, known-sensitive
    names, binaries, private-key headers and common token patterns; it is not a
    general-purpose secret scanner. `hosting/README.md` is excluded because it
    contains a PEM-shaped example. Do not transfer the full tracked tree.
    No untracked, ignored or working-tree files are permitted. From
    `portfolio/`, create a private scratch directory **outside both repos**
    with mode 0700 and export a curated archive and NUL-delimited Git blob
    manifest from the checked main commit:

   ```sh
    umask 077
    mkdir -m 700 "$HOME/.local/share/openwebui-agent/infra-snapshots"
    python3 k8s/code-agent/infra-review/create-snapshot.py ../infra \
      "$HOME/.local/share/openwebui-agent/infra-snapshots"
    # Set SNAP_DIR to the printed snapshot_dir after reviewing its path.
    MAIN_SHA=$(git -C ../infra rev-parse HEAD)
    SNAPSHOT="$SNAP_DIR/snapshot.tar"
    MANIFEST="$SNAP_DIR/ls-tree.bin"
    shasum -a 256 "$SNAPSHOT"
   ```

    If the parent directory exists, confirm it is 0700 rather than running
    `mkdir` again. The exporter creates a fresh private directory and checks
    that the archive's regular-file members match the audited subset **before**
    printing its checksum; do not substitute `git archive` over the whole repo.
    Match its printed `main_sha` to `MAIN_SHA` and its printed
    `archive_sha256` to `shasum`. Preserve the checksum and SHA in a private
    operator record. Stop if HEAD or clean status changed. Do not paste private
    content or manifests into tickets. Create the non-secret
   SHA ConfigMap **out of git**, after the
   audit, using `kubectl -n code-agent create configmap infra-review-snapshot
   --from-literal=MAIN_SHA="$MAIN_SHA"`; never commit the private SHA in public
   YAML. If one exists, stop the pods and deliberately replace the prior
   ConfigMap only during the refresh procedure below; do not merge revisions.
   Do not put source or credentials in the ConfigMap.

   Create `infra-terminal-api-key` out of git using a private temporary file
   and `kubectl -n code-agent create secret generic infra-terminal-api-key
   --from-file=OPEN_TERMINAL_API_KEY="$KEY_FILE"` (generate `KEY_FILE` via
   `mktemp` and `openssl rand -hex 32`; keep tracing off, never print the key,
   delete temporary files after configuring the approved WebUI connection).
   `kubectl -n code-agent get secret infra-terminal-api-key code-agent-github-app`
   verifies names only; never display Secret data. Do not overwrite either
   Secret without a rotation plan.
 3. After the reviewed PR is merged, digest, SHA contract and infra App
   repo-scoped authorization are verified, explicitly approve the overlay,
   create the broker Service **before** creating a terminal Pod, apply **only**
   `kubectl apply -k k8s/code-agent/infra-review` from `portfolio/`. This
   provisions the private PVC and services while leaving both replicas at 0.
    Keep **both at 0** until the review-only broker code and pinned image are
    verified against the imported local digest before scaling it.
   Only then set broker replicas to 1, re-render/apply, and check
   `kubectl -n code-agent rollout status deployment/infra-pr-broker --timeout=300s`.
   Confirm absent/mismatched snapshot SHA, public PR publication and portfolio
   submissions are refused before connecting the terminal.
4. Set terminal replicas to 1 in the reviewed manifest, re-render/apply and
   check `kubectl -n code-agent rollout status deployment/infra-terminal
   --timeout=300s`. Check that `INFRA_PR_BROKER_SERVICE_HOST` was injected;
   if missing, scale back to 0, create/check broker Service, then recreate the
   terminal Pod (Service env injection happens only on creation). From the Mac
    copy **only the curated** archive and matching manifest to the separate PVC through the pod;
   no hostPath, `.git` mount, direct Git credentials or reusable tree:

   ```sh
   POD=$(kubectl -n code-agent get pod -l app=infra-terminal -o jsonpath='{.items[0].metadata.name}')
   kubectl -n code-agent exec "$POD" -- mkdir /home/user/infra
   kubectl cp "$SNAPSHOT" "code-agent/$POD:/home/user/infra-snapshot.tar"
   kubectl cp "$MANIFEST" "code-agent/$POD:/home/user/infra-ls-tree.bin"
   kubectl -n code-agent exec "$POD" -- sha256sum /home/user/infra-snapshot.tar
   ```

   **Stop here** and compare the pod checksum against the Mac's private
   SHA-256 record. If it differs, delete transferred files and stop. Only
   after they match, extract into the **new empty directory** and verify every
   file against the audited tracked git blob manifest with the tracked verifier:

   ```sh
   kubectl -n code-agent exec "$POD" -- tar -xf /home/user/infra-snapshot.tar -C /home/user/infra
   kubectl -n code-agent exec -i "$POD" -- python3 - /home/user/infra /home/user/infra-ls-tree.bin < k8s/code-agent/infra-review/verify-snapshot.py
   kubectl cp k8s/code-agent/infra-review/infra-terminal-AGENTS.md "code-agent/$POD:/home/user/AGENTS.md"
   kubectl -n code-agent exec "$POD" -- rm /home/user/infra-snapshot.tar /home/user/infra-ls-tree.bin
   ```

   Do not enable the WebUI connection until blob verification succeeds. Remove
   the Mac scratch files after operator-retention needs are met. On **every
   refresh**, first disable the WebUI connection, scale **both** Deployments to
   zero and wait for pods to terminate. Audit a fresh clean main commit; retain
   a secure private backup only if needed, remove the old extracted tree and
   staging files deliberately (never reuse or merge directories), replace the
   operator-controlled ConfigMap with the newly audited `MAIN_SHA` (with both
   Deployments stopped: `kubectl -n code-agent delete configmap infra-review-snapshot`
   then `kubectl -n code-agent create configmap infra-review-snapshot
   --from-literal=MAIN_SHA="$MAIN_SHA"`), and restart
   only the reviewed workloads to create a **new empty** `/home/user/infra`.
   Copy/check/extract/verify the new archive as above **before** reconnecting
   WebUI or allowing proposals. Ensure the broker reads the same SHA after its
   restart. Stop on any failure; never trust modified PVC contents as baseline.
   No private source or PEM belongs in ConfigMaps, logs or tickets.
5. In the existing public Open WebUI add an **administrator-only system**
   Open Terminal connection to `http://infra-terminal.code-agent.svc.cluster.local:8000`
   with the separate terminal key. Keep it disabled and grant **no default,
   shared or non-admin access**. Before first use, use an approved non-admin
   account to negatively test both listing and invoking the connection (and
   test pending accounts); remove/disable the connection immediately on any
   access. Enable only for the approved admin after denial is proven. Do not
   expose a broker tool/endpoint directly in WebUI. Test a read-only admin
   review and verify no PR, deployment or public artifact is produced.

## Rollback and post-change checks

On any failure **first** disable/remove the infra system connection and all
grants in WebUI, then `kubectl -n code-agent scale deployment/infra-terminal
deployment/infra-pr-broker --replicas=0`. Verify both have zero pods and
non-admin/admin calls fail. Preserve `infra-terminal-home` PVC by default;
do **not** run `kubectl delete -k k8s/code-agent/infra-review` without first
backing up and explicitly authorizing PVC deletion and checking the PV reclaim
policy. To remove only compute/network resources, delete the named infra
Deployments, Services and NetworkPolicies after scale-down, leaving the PVC
and Secrets under operator control. Rotate/revoke exposed API key or App PEM
if compromised. Restoring an earlier broker image is not safe unless it
implements review mode; the safe rollback is stopped workloads.

After a staged change: `kubectl -n code-agent get deployment,pod,svc,pvc,networkpolicy`
and `kubectl -n code-agent get endpoints infra-terminal infra-pr-broker`; confirm
PVC Bound, 1/1 only after approved activation, no restart/resource pressure,
ClusterIP services and no new Ingress/IngressRoute/NodePort/LoadBalancer.
Inspect redacted logs (`kubectl -n code-agent logs deployment/infra-pr-broker`)
   for accidental private content. From approved diagnostic pods verify terminal
   DNS (UDP/TCP 53) and public HTTPS fail, injected Service IP-to-broker 8001
   works, OpenWebUI-to-broker and portfolio
terminal-to-infra-broker fail, other pods cannot reach the infra terminal,
   infra terminal cannot reach DNS/public HTTPS/cluster/private/Tailscale endpoints,
and broker cannot reach private/Tailscale/cluster targets but can reach GitHub
HTTPS. Remove diagnostic pods. Repeat negative non-admin and pending-user
WebUI tests, verify only private review proposals with the expected SHA and
no PR publication/deployment. Check existing portfolio terminal and broker
still function with their original boundaries; stop and roll back on any
boundary failure.
