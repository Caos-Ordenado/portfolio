#!/bin/bash
set -euo pipefail

# Build natively on caos (amd64 BuildKit) and deploy to the home cluster.
# Only the build context is uploaded; see scripts/caos-build.sh at the repo root.

cd "$(dirname "$0")"
REPO_ROOT="$(git -C ../.. rev-parse --show-superproject-working-tree 2>/dev/null || true)"
CAOS_BUILD="${CAOS_BUILD:-${REPO_ROOT:-../../..}/scripts/caos-build.sh}"
IMAGE_NAME="openwebui-tools"
IMAGE_TAG="dev-$(date +%Y%m%d%H%M%S)"
K8S_NS="default"
K8S_DEPLOY="openwebui-tools"
K8S_DIR="../../k8s/openwebui_tools"
DEPLOYMENT_FILE="$K8S_DIR/deployment.yaml"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

[ -x "$CAOS_BUILD" ] || { echo "❌ Error: $CAOS_BUILD not found"; exit 1; }

echo "📦 Preparing build context (shared + openwebui_tools)..."
cp -r ../../shared/shared "$TEMP_DIR/shared"
mkdir -p "$TEMP_DIR/openwebui_tools"
cp -r ./* "$TEMP_DIR/openwebui_tools/"
rm -f "$TEMP_DIR/openwebui_tools/.env" "$TEMP_DIR/shared/.env"

echo "🏗️  Building ${IMAGE_NAME}:${IMAGE_TAG} natively on caos..."
"$CAOS_BUILD" "$TEMP_DIR" openwebui_tools/Dockerfile "${IMAGE_NAME}:${IMAGE_TAG}"

# Pin the new tag in the manifest so `kubectl apply -k` stays in sync (commit it)
sed -i.bak -E "s|image: ${IMAGE_NAME}:[^[:space:]]+|image: ${IMAGE_NAME}:${IMAGE_TAG}|" "$DEPLOYMENT_FILE" && rm -f "$DEPLOYMENT_FILE.bak"

echo "⚙️ Applying Kubernetes configurations..."
kubectl apply -k "$K8S_DIR"

echo "⏳ Waiting for rollout..."
if kubectl rollout status deployment/${K8S_DEPLOY} -n ${K8S_NS} --timeout=300s; then
  echo "✅ ${IMAGE_NAME}:${IMAGE_TAG} deployed (remember to commit $DEPLOYMENT_FILE)."
else
  echo "❌ Rollout failed. Logs: kubectl logs -n ${K8S_NS} -l app=${K8S_DEPLOY} --tail=100"
  echo "↩️  Roll back with: kubectl rollout undo deployment/${K8S_DEPLOY} -n ${K8S_NS}"
  exit 1
fi
