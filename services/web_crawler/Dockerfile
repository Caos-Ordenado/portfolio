FROM python:3.13-slim

# Create a non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Upgrade pip and set optimizations
RUN pip install --upgrade pip

# Set pip cache directory and other optimizations
ENV PIP_NO_CACHE_DIR=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PYTHONPATH="/app:/app/shared"

# Copy and install shared module first (this provides base dependencies)
COPY shared/ /app/shared/
RUN cd /app/shared && \
    pip install --no-cache-dir -e . && \
    pip list | grep -E "(shared|sqlalchemy|redis|aiohttp|loguru)" | head -10

# Copy web crawler files  
COPY web_crawler/src/ /app/src/
COPY web_crawler/pyproject.toml /app/pyproject.toml
COPY web_crawler/README.md /app/README.md

# Install web crawler dependencies (only web crawler specific deps due to optimized pyproject.toml)
RUN pip install --no-cache-dir -e . && \
    pip list | grep -E "(fastapi|uvicorn|lxml|psutil|playwright)" | head -10

# Change ownership of app directory to appuser
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Ensure user binaries are in PATH
ENV PATH="/home/appuser/.local/bin:$PATH"

# Set the working directory to /app (not /app/src)
WORKDIR /app

# Expose the FastAPI port
EXPOSE 8000

# Run the application using uvicorn as a module
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"] 