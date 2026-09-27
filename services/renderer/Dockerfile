FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

# Ensure PYTHONPATH includes app and shared source (mirrors web_crawler pattern)
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH="/app:/app/shared"

# Copy shared package and install editable (keeps source available like web_crawler)
COPY shared/ /app/shared/
RUN cd /app/shared && \
    pip install --no-cache-dir -e . && \
    pip list | grep -E "(shared|sqlalchemy|redis|aiohttp|loguru)" | head -10

# Copy renderer files
COPY renderer/src /app/src
COPY renderer/pyproject.toml /app/pyproject.toml

# Install renderer deps
RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]



