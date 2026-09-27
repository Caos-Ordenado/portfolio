#!/bin/bash
set -euo pipefail

# Build natively on caos (amd64 BuildKit) and deploy to the home cluster.
# Only the build context is uploaded; see scripts/caos-build.sh at the repo root.

cd "$(dirname "$0")"
REPO_ROOT="$(git -C ../.. rev-parse --show-superproject-working-tree 2>/dev/null || true)"
CAOS_BUILD="${CAOS_BUILD:-${REPO_ROOT:-../../..}/scripts/caos-build.sh}"
IMAGE_NAME="renderer"
IMAGE_TAG="dev-$(date +%Y%m%d%H%M%S)"
K8S_NS="default"
K8S_DEPLOY="renderer"
K8S_DIR="../../k8s/renderer"
DEPLOYMENT_FILE="$K8S_DIR/deployment.yaml"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

# Record the deployed tag in git: commit ONLY the manifest (other local changes are left alone)
# and push it. Set DEPLOY_PUSH=0 to commit without pushing, DEPLOY_COMMIT=0 to skip both.
pin_manifest_in_git() {
  [ "${DEPLOY_COMMIT:-1}" = "1" ] || { echo "ℹ️  DEPLOY_COMMIT=0: remember to commit $DEPLOYMENT_FILE"; return 0; }
  if git diff --quiet -- "$DEPLOYMENT_FILE"; then return 0; fi
  git commit -q -m "Deploy ${IMAGE_NAME}:${IMAGE_TAG}" -- "$DEPLOYMENT_FILE"
  echo "📝 Committed $DEPLOYMENT_FILE ($(git rev-parse --short HEAD))"
  if [ "${DEPLOY_PUSH:-1}" = "1" ]; then
    git push -q origin HEAD && echo "⬆️  Pushed to origin" || echo "⚠️  Push failed; run 'git push' manually"
  fi
}

[ -x "$CAOS_BUILD" ] || { echo "❌ Error: $CAOS_BUILD not found"; exit 1; }

echo "📦 Preparing build context (shared + renderer)..."
cp -r ../../shared/shared "$TEMP_DIR/shared"
mkdir -p "$TEMP_DIR/renderer"
cp -r ./* "$TEMP_DIR/renderer/"
rm -f "$TEMP_DIR/renderer/.env" "$TEMP_DIR/shared/.env"

echo "🏗️  Building ${IMAGE_NAME}:${IMAGE_TAG} natively on caos..."
"$CAOS_BUILD" "$TEMP_DIR" renderer/Dockerfile "${IMAGE_NAME}:${IMAGE_TAG}"

# Pin the new tag in the manifest so `kubectl apply -k` stays in sync (committed after a successful rollout)
sed -i.bak -E "s|image: ${IMAGE_NAME}:[^[:space:]]+|image: ${IMAGE_NAME}:${IMAGE_TAG}|" "$DEPLOYMENT_FILE" && rm -f "$DEPLOYMENT_FILE.bak"

echo "⚙️ Applying Kubernetes configurations..."
kubectl apply -k "$K8S_DIR"

echo "⏳ Waiting for rollout..."
if kubectl rollout status deployment/${K8S_DEPLOY} -n ${K8S_NS} --timeout=300s; then
  pin_manifest_in_git
  echo "✅ ${IMAGE_NAME}:${IMAGE_TAG} deployed."
else
  git checkout -q -- "$DEPLOYMENT_FILE"
  echo "❌ Rollout failed. Logs: kubectl logs -n ${K8S_NS} -l app=${K8S_DEPLOY} --tail=100"
  echo "↩️  Roll back with: kubectl rollout undo deployment/${K8S_DEPLOY} -n ${K8S_NS}"
  exit 1
fi
