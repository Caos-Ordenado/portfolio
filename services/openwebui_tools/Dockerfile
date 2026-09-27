FROM python:3.13-slim

# Create a non-root user (matches other agents)
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

ENV PIP_NO_CACHE_DIR=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PYTHONPATH="/app:/app/shared"

# Copy and install shared module first
COPY shared/ /app/shared/
RUN cd /app/shared && pip install --no-cache-dir -e .

# Copy tool proxy files
COPY openwebui_tools/src/ /app/src/
COPY openwebui_tools/pyproject.toml /app/pyproject.toml
COPY openwebui_tools/README.md /app/README.md

RUN pip install --no-cache-dir -e .

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]


