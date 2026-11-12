# Concurro's credentials vault: FastAPI on uvicorn with pinned dependencies, run unprivileged.
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d
COPY requirements.txt /tmp/vault-requirements.txt
RUN pip install --no-cache-dir -r /tmp/vault-requirements.txt \
    && rm /tmp/vault-requirements.txt \
    && mkdir /data && chown 65532:65532 /data
COPY vault.py /app/vault.py
USER 65532:65532
ENTRYPOINT ["python3", "-I", "-B", "/app/vault.py"]
