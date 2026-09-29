FROM python:3.13-slim@sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332

# Create a non-root user (matches other agents)
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

ENV PIP_NO_CACHE_DIR=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PYTHONPATH="/app:/app/shared"

# Install only the audited, hashed Linux amd64 runtime wheels (no build backend).
COPY openwebui_tools/requirements.lock /app/requirements.lock
RUN pip install --require-hashes --only-binary=:all: -r /app/requirements.lock

# Import the local packages directly via PYTHONPATH; do not pip-install their
# unpinned build-system requirements.
COPY shared/shared/ /app/shared/shared/
COPY openwebui_tools/src/ /app/src/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

