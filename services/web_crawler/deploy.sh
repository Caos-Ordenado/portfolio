#!/bin/bash

# Exit on any error
set -e

# Simple deployment script that builds for linux/amd64 and deploys to home server

# Configuration
REMOTE_USER="${REMOTE_USER:-caos}"
REMOTE_HOST="${REMOTE_HOST:-home.server}"
REMOTE_PASS="${REMOTE_PASS:?Error: REMOTE_PASS environment variable not set}"
IMAGE_NAME="web-crawler"
IMAGE_TAG="latest"
K8S_DIR="../../k8s/web_crawler"

# Function to clean up temporary files
cleanup() {
    rm -f /tmp/${IMAGE_NAME}.tar /tmp/${IMAGE_NAME}.tar.gz
    rm -f /tmp/web-crawler-config.env
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}

# Set up cleanup on script exit
trap cleanup EXIT

echo "🚀 Starting deployment process..."

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ Error: .env file not found in current directory"
    exit 1
fi

# Update ConfigMap first
echo "📝 Updating ConfigMap..."
grep -v "PASSWORD\|USER" .env | grep -v "^\s*#" | grep "=" > /tmp/web-crawler-config.env
if [ -s /tmp/web-crawler-config.env ]; then
    kubectl create configmap web-crawler-config --from-env-file=/tmp/web-crawler-config.env -n default --dry-run=client -o yaml | kubectl apply -f -
    echo "✅ ConfigMap updated successfully"
else
    echo "⚠️  Warning: No configuration found in .env file"
fi

# Build Docker image
echo "📦 Building Docker image..."
# Create a temporary directory for the build context
TEMP_DIR=$(mktemp -d)
echo "Using temporary directory: $TEMP_DIR"

# Copy the shared module
echo "Copying shared module..."
cp -r ../../shared/shared "$TEMP_DIR/"

# Copy the web crawler files
echo "Copying web crawler files..."
mkdir -p "$TEMP_DIR/web_crawler"
cp -r ./* "$TEMP_DIR/web_crawler/"

# Provide a .dockerignore at the build context root to shrink context (if present locally)
if [ -f ".dockerignore" ]; then
  cp .dockerignore "$TEMP_DIR/.dockerignore"
else
  # Create a sensible default .dockerignore to speed up build context upload
  cat > "$TEMP_DIR/.dockerignore" <<'EOF'
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
web_crawler/.env
k8s/**
EOF
fi

# Build the image from the temporary directory with retry logic
echo "Building Docker image (this may take a few minutes)..."
cd "$TEMP_DIR"

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

        echo "❌ Error: docker buildx is not available (required to build linux/amd64 images from an Apple Silicon daemon)."
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

# Always build for linux/amd64 (Ubuntu server architecture)
BUILD_PLATFORM="linux/amd64"
echo "🎯 Building for server platform: $BUILD_PLATFORM"

# Build for linux/amd64 platform (use buildx for cross-arch builds on Apple Silicon)
echo "Building Docker image with build optimizations..."
if ensure_buildx_builder; then
    if docker buildx build \
        --platform "$BUILD_PLATFORM" \
        --load \
        -f web_crawler/Dockerfile \
        -t ${IMAGE_NAME}:${IMAGE_TAG} .; then
        echo "✅ Docker image built successfully"
        
        # Show image size for monitoring
        IMAGE_SIZE=$(docker images ${IMAGE_NAME}:${IMAGE_TAG} --format "table {{.Size}}" | tail -n 1)
        echo "📊 Image size: $IMAGE_SIZE"
    else
        echo "❌ Docker build failed"
        cd - > /dev/null
        exit 1
    fi
else
    # Fallback path: buildx missing, but daemon is amd64 so plain build works.
    if docker build \
        --platform "$BUILD_PLATFORM" \
        -f web_crawler/Dockerfile \
        -t ${IMAGE_NAME}:${IMAGE_TAG} .; then
        echo "✅ Docker image built successfully (without buildx)"
        
        # Show image size for monitoring
        IMAGE_SIZE=$(docker images ${IMAGE_NAME}:${IMAGE_TAG} --format "table {{.Size}}" | tail -n 1)
        echo "📊 Image size: $IMAGE_SIZE"
    else
        echo "❌ Docker build failed"
        cd - > /dev/null
        exit 1
    fi
fi

# Return to original directory
cd - > /dev/null

# Save and transfer image (compressed)
echo "💾 Saving and compressing Docker image..."
if command -v pigz >/dev/null 2>&1; then
  # Use pigz for faster parallel compression when available
  docker save ${IMAGE_NAME}:${IMAGE_TAG} | pigz -c > /tmp/${IMAGE_NAME}.tar.gz
else
  docker save ${IMAGE_NAME}:${IMAGE_TAG} | gzip -c > /tmp/${IMAGE_NAME}.tar.gz
fi

echo "📤 Copying compressed image to home server..."
if sshpass -p "${REMOTE_PASS}" scp -C /tmp/${IMAGE_NAME}.tar.gz ${REMOTE_USER}@${REMOTE_HOST}:/tmp/; then
    echo "✅ Image copied successfully"
else
    echo "❌ Failed to copy image to remote server"
    exit 1
fi

echo "📥 Importing image into microk8s (decompressing on remote)..."
if sshpass -p "${REMOTE_PASS}" ssh ${REMOTE_USER}@${REMOTE_HOST} "set -e; echo '${REMOTE_PASS}' | sudo -S sh -c 'gunzip -c /tmp/${IMAGE_NAME}.tar.gz | microk8s ctr image import -'; rm -f /tmp/${IMAGE_NAME}.tar.gz"; then
    echo "✅ Image imported successfully"
else
    echo "❌ Failed to import image on remote server"
    exit 1
fi

# Apply k8s configurations
echo "⚙️ Applying Kubernetes configurations..."
if kubectl apply -k ${K8S_DIR}; then
    echo "✅ Kubernetes configurations applied"
else
    echo "❌ Failed to apply Kubernetes configurations"
    exit 1
fi

# Restart and wait for deployment
echo "🔄 Forcing a rollout restart..."
kubectl rollout restart deployment/web-crawler -n default

echo "⏳ Waiting for deployment to roll out..."
if kubectl rollout status deployment/web-crawler -n default --timeout=300s; then
    echo "✅ Deployment completed successfully!"
    echo "🌐 The web crawler is accessible at: http://home.server:30080/crawler/"
    echo "📝 Check the logs with: kubectl logs -n default -l app=web-crawler --tail=100"
else
    echo "❌ Deployment rollout timed out or failed"
    echo "📝 Check the logs with: kubectl logs -n default -l app=web-crawler --tail=100"
    exit 1
fi 