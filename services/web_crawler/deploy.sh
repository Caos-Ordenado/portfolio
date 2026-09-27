#!/bin/bash

# Exit on any error
set -euo pipefail

# Build natively on caos (amd64 BuildKit) and deploy to the home cluster.
# Only the build context is uploaded; see scripts/caos-build.sh at the repo root.

cd "$(dirname "$0")"
REPO_ROOT="$(git -C ../.. rev-parse --show-superproject-working-tree 2>/dev/null || true)"
CAOS_BUILD="${CAOS_BUILD:-${REPO_ROOT:-../../..}/scripts/caos-build.sh}"
IMAGE_NAME="web-crawler"
IMAGE_TAG="dev-$(date +%Y%m%d%H%M%S)"
K8S_DIR="../../k8s/web_crawler"
DEPLOYMENT_FILE="$K8S_DIR/deployment.yaml"
CONFIG_ENV="$(mktemp)"
TEMP_DIR="$(mktemp -d)"

cleanup() {
    rm -f "$CONFIG_ENV"
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

echo "🚀 Starting deployment process..."

if [ ! -f ".env" ]; then
    echo "❌ Error: .env file not found in current directory"
    exit 1
fi
[ -x "$CAOS_BUILD" ] || { echo "❌ Error: $CAOS_BUILD not found"; exit 1; }

# Update ConfigMap first (non-secret values only)
echo "📝 Updating ConfigMap..."
grep -v "PASSWORD\|USER" .env | grep -v "^\s*#" | grep "=" > "$CONFIG_ENV" || true
if [ -s "$CONFIG_ENV" ]; then
    kubectl create configmap web-crawler-config --from-env-file="$CONFIG_ENV" -n default --dry-run=client -o yaml | kubectl apply -f -
    echo "✅ ConfigMap updated successfully"
else
    echo "⚠️  Warning: No configuration found in .env file"
fi

# Build context: shared package + this service
echo "📦 Preparing build context..."
cp -r ../../shared/shared "$TEMP_DIR/"
mkdir -p "$TEMP_DIR/web_crawler"
cp -r ./* "$TEMP_DIR/web_crawler/"
rm -f "$TEMP_DIR/web_crawler/.env" "$TEMP_DIR/shared/.env"

echo "🏗️  Building ${IMAGE_NAME}:${IMAGE_TAG} natively on caos..."
"$CAOS_BUILD" "$TEMP_DIR" web_crawler/Dockerfile "${IMAGE_NAME}:${IMAGE_TAG}"

# Pin the new tag in the manifest so `kubectl apply -k` stays in sync (commit it)
sed -i.bak -E "s|image: ${IMAGE_NAME}:[^[:space:]]+|image: ${IMAGE_NAME}:${IMAGE_TAG}|" "$DEPLOYMENT_FILE" && rm -f "$DEPLOYMENT_FILE.bak"

echo "⚙️ Applying Kubernetes configurations..."
kubectl apply -k "$K8S_DIR"

echo "⏳ Waiting for deployment to roll out..."
if kubectl rollout status deployment/web-crawler -n default --timeout=300s; then
    echo "✅ Deployment completed successfully! (${IMAGE_NAME}:${IMAGE_TAG}, remember to commit $DEPLOYMENT_FILE)"
    echo "🌐 The web crawler is accessible at: http://home.server:30080/crawler/"
else
    echo "❌ Deployment rollout timed out or failed"
    echo "📝 Check the logs with: kubectl logs -n default -l app=web-crawler --tail=100"
    echo "↩️  Roll back with: kubectl rollout undo deployment/web-crawler -n default"
    exit 1
fi
