#!/bin/bash
set -e
set -o pipefail

# Ensure relative paths resolve from this script dir
cd "$(dirname "$0")"

# Config (reuse home server settings)
REMOTE_USER="${REMOTE_USER:-caos}"
REMOTE_HOST="${REMOTE_HOST:-home.server}"
REMOTE_PASS="${REMOTE_PASS:?Error: REMOTE_PASS environment variable not set}"
IMAGE_NAME="renderer"
# Unique tag to force rollout to pick new image
IMAGE_TAG="dev-$(date +%Y%m%d%H%M%S)"
K8S_DIR="../../k8s/renderer"

# Always build for linux/amd64 (Ubuntu server architecture)
BUILD_PLATFORM="linux/amd64"
TAR_GZ_PATH="/tmp/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"

cleanup() {
  rm -f "${TAR_GZ_PATH}" || true
  [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ] && rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

echo "🚀 Building and deploying renderer..."

# Ensure buildx is available (required for cross-platform builds on Apple Silicon)
ensure_buildx_builder() {
  local builder_name="${BUILDX_BUILDER_NAME:-portfolio-builder}"

  if ! docker buildx version >/dev/null 2>&1; then
    # If we're already on an amd64 Docker daemon (e.g. Colima started with --arch x86_64),
    # we can build without buildx and without emulation.
    local docker_arch
    docker_arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
    if [[ "$docker_arch" == "x86_64" || "$docker_arch" == "amd64" ]]; then
      echo "⚠️  docker buildx is not available, but Docker daemon architecture is '$docker_arch'."
      echo "   Proceeding without buildx (native amd64 daemon)."
      return 1
    fi

    echo "❌ Error: docker buildx is not available (required to build ${BUILD_PLATFORM} images from an Apple Silicon daemon)."
    echo
    echo "Fix options (pick one):"
    echo "  1) Install/enable buildx plugin (recommended):"
    echo "     - Homebrew: brew install docker-buildx"
    echo "     - Then make Docker find it (pick one):"
    echo "         a) Add to ~/.docker/config.json:"
    echo "            { \"cliPluginsExtraDirs\": [\"/opt/homebrew/lib/docker/cli-plugins\"] }"
    echo "         b) Or symlink it:"
    echo "            mkdir -p ~/.docker/cli-plugins"
    echo "            ln -sf /opt/homebrew/lib/docker/cli-plugins/docker-buildx ~/.docker/cli-plugins/docker-buildx"
    echo "     - Verify: docker buildx version"
    echo
    echo "  2) Run Colima as amd64 (avoids buildx/emulation):"
    echo "     - colima stop"
    echo "     - colima start --arch x86_64"
    echo
    echo "Current Docker daemon architecture: '${docker_arch:-unknown}'"
    exit 1
  fi

  # Create a dedicated builder if it doesn't exist
  if ! docker buildx inspect "$builder_name" >/dev/null 2>&1; then
    docker buildx create --name "$builder_name" --driver docker-container --use >/dev/null
  else
    docker buildx use "$builder_name" >/dev/null
  fi

  # Bootstrap the builder (also surfaces emulation issues early)
  docker buildx inspect --bootstrap >/dev/null
}

# Prepare temp build context
TEMP_DIR=$(mktemp -d)
echo "Using temp dir: $TEMP_DIR"

echo "Copying shared module..."
cp -r ../../shared/shared "$TEMP_DIR/shared"

echo "Copying renderer files..."
mkdir -p "$TEMP_DIR/renderer"
cp -r ./* "$TEMP_DIR/renderer/"

cd "$TEMP_DIR"

echo "🎯 Building image for ${BUILD_PLATFORM} with tag ${IMAGE_NAME}:${IMAGE_TAG}"

# Provide a .dockerignore at the build context root to shrink context (if present locally)
if [ -f "renderer/.dockerignore" ]; then
  cp "renderer/.dockerignore" ".dockerignore"
else
  # Create a sensible default .dockerignore to speed up build context upload
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
renderer/.env
k8s/**
EOF
fi

echo "Building Docker image with build optimizations..."
if ensure_buildx_builder; then
  if docker buildx build \
    --platform "${BUILD_PLATFORM}" \
    --load \
    --cache-from ${IMAGE_NAME}:${IMAGE_TAG} \
    --build-arg BUILDKIT_INLINE_CACHE=1 \
    -f renderer/Dockerfile \
    -t ${IMAGE_NAME}:${IMAGE_TAG} .; then
    echo "✅ Image built successfully"
  else
    echo "❌ Build failed"; exit 1
  fi
else
  # Fallback path: buildx missing, but daemon is amd64 so plain build works.
  if docker build \
    --platform "${BUILD_PLATFORM}" \
    --cache-from ${IMAGE_NAME}:${IMAGE_TAG} \
    --build-arg BUILDKIT_INLINE_CACHE=1 \
    -f renderer/Dockerfile \
    -t ${IMAGE_NAME}:${IMAGE_TAG} .; then
    echo "✅ Image built successfully (without buildx)"
  else
    echo "❌ Build failed"; exit 1
  fi
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

echo "⚙️ Applying Kubernetes manifests..."
kubectl apply -k ${K8S_DIR}

# Point deployment to the freshly imported tag
echo "🔧 Updating deployment image to ${IMAGE_NAME}:${IMAGE_TAG}..."
kubectl set image deployment/renderer renderer=${IMAGE_NAME}:${IMAGE_TAG} -n default

echo "🔄 Restarting deployment..."
kubectl rollout restart deployment/renderer -n default

echo "⏳ Waiting for rollout..."
kubectl rollout status deployment/renderer -n default --timeout=300s

echo "✅ Renderer deployed."
echo "🌐 Test health: curl http://home.server:30080/renderer/health"

