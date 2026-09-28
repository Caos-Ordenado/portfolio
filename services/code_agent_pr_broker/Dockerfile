# Verified with: docker buildx imagetools inspect python:3.13-slim
# linux/amd64 manifest for Python 3.13.15-slim-trixie (not the multi-arch index).
FROM python:3.13-slim@sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332
WORKDIR /app
COPY requirements.lock /app/
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock
# The app package has no build-time artifacts; copy only its Python source.
# Do not resolve/install it with pip, which could pull unpinned build dependencies.
COPY broker/ /app/broker/
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8001
CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "8001"]
