#!/bin/bash
set -e
set -o pipefail

# Ensure relative paths resolve from this script dir
cd "$(dirname "$0")"

# Config (reuse home server settings)
REMOTE_USER="${REMOTE_USER:-caos}"
REMOTE_HOST="${REMOTE_HOST:-home.server}"
REMOTE_PASS="${REMOTE_PASS:?Error: REMOTE_PASS environment variable not set}"

IMAGE_NAME="openwebui-tools"
# Unique tag to force rollout to pick new image
IMAGE_TAG="dev-$(date +%Y%m%d%H%M%S)"
K8S_NS="default"
K8S_DEPLOY="openwebui-tools"

# Always build for linux/amd64 (Ubuntu server architecture)
BUILD_PLATFORM="linux/amd64"
TAR_GZ_PATH="/tmp/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"

cleanup() {
  rm -f "${TAR_GZ_PATH}" || true
  [ -n "${TEMP_DIR:-}" ] && [ -d "${TEMP_DIR:-}" ] && rm -rf "$TEMP_DIR" || true
}
trap cleanup EXIT

ensure_buildx_builder() {
  local builder_name="${BUILDX_BUILDER_NAME:-portfolio-builder}"

  if ! docker buildx version >/dev/null 2>&1; then
    local docker_arch
    docker_arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
    if [[ "$docker_arch" == "x86_64" || "$docker_arch" == "amd64" ]]; then
      echo "⚠️  docker buildx not available, but daemon arch is '$docker_arch'. Proceeding without buildx."
      return 1
    fi
    echo "❌ Error: docker buildx is required to build ${BUILD_PLATFORM} images from an Apple Silicon daemon."
    exit 1
  fi

  if ! docker buildx inspect "$builder_name" >/dev/null 2>&1; then
    docker buildx create --name "$builder_name" --driver docker-container --use >/dev/null
  else
    docker buildx use "$builder_name" >/dev/null
  fi
  docker buildx inspect --bootstrap >/dev/null
}

echo "🚀 Building and deploying ${IMAGE_NAME}..."

TEMP_DIR=$(mktemp -d)
echo "Using temp dir: $TEMP_DIR"

echo "Copying shared module..."
# Avoid copying any local .env that may exist in the shared dir (not needed for builds)
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude '.env' --exclude '__pycache__' ../../shared/shared/ "$TEMP_DIR/shared/"
else
  cp -r ../../shared/shared "$TEMP_DIR/shared"
  rm -f "$TEMP_DIR/shared/.env" || true
fi

echo "Copying openwebui_tools files..."
mkdir -p "$TEMP_DIR/openwebui_tools"
cp -r ./* "$TEMP_DIR/openwebui_tools/"

cd "$TEMP_DIR"

echo "🎯 Building image for ${BUILD_PLATFORM} with tag ${IMAGE_NAME}:${IMAGE_TAG}"

# Provide a .dockerignore at build context root to shrink context
cat > ".dockerignore" <<'EOF'
.git
**/__pycache__/**
**/*.pyc
**/*.pyo
**/*.pyd
**/.mypy_cache/**
**/.pytest_cache/**
**/.ruff_cache/**
**/.venv/**
**/venv/**
**/env/**
**/dist/**
**/build/**
**/*.egg-info/**
.DS_Store
openwebui_tools/.env
k8s/**
EOF

if ensure_buildx_builder; then
  docker buildx build \
    --platform "${BUILD_PLATFORM}" \
    --load \
    -f openwebui_tools/Dockerfile \
    -t ${IMAGE_NAME}:${IMAGE_TAG} .
else
  docker build \
    --platform "${BUILD_PLATFORM}" \
    -f openwebui_tools/Dockerfile \
    -t ${IMAGE_NAME}:${IMAGE_TAG} .
fi

cd - >/dev/null

echo "💾 Saving and compressing image..."
if command -v pigz >/dev/null 2>&1; then
  docker save ${IMAGE_NAME}:${IMAGE_TAG} | pigz -c > "${TAR_GZ_PATH}"
else
  docker save ${IMAGE_NAME}:${IMAGE_TAG} | gzip -c > "${TAR_GZ_PATH}"
fi

echo "📤 Copying image to remote..."
sshpass -p "${REMOTE_PASS}" scp -C "${TAR_GZ_PATH}" ${REMOTE_USER}@${REMOTE_HOST}:/tmp/

echo "📥 Importing into microk8s..."
sshpass -p "${REMOTE_PASS}" ssh ${REMOTE_USER}@${REMOTE_HOST} "set -e; echo '${REMOTE_PASS}' | sudo -S sh -c 'gunzip -c /tmp/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz | microk8s ctr image import -'; rm -f /tmp/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"

echo "🔧 Updating deployment image to ${IMAGE_NAME}:${IMAGE_TAG}..."
kubectl -n ${K8S_NS} set image deployment/${K8S_DEPLOY} ${K8S_DEPLOY}=${IMAGE_NAME}:${IMAGE_TAG}

echo "🔄 Restarting deployment..."
kubectl rollout restart deployment/${K8S_DEPLOY} -n ${K8S_NS}

echo "⏳ Waiting for rollout..."
kubectl rollout status deployment/${K8S_DEPLOY} -n ${K8S_NS} --timeout=300s

echo "✅ ${IMAGE_NAME} deployed."


