FROM python:3.13-slim@sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332
WORKDIR /app
COPY diagnostics/ /app/diagnostics/
RUN useradd --uid 10001 --create-home diagnostic
USER diagnostic
EXPOSE 8002
CMD ["python", "-m", "diagnostics.server"]
