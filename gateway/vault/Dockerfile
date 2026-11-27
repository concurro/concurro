# Concurro's credentials vault: FastAPI on uvicorn with pinned dependencies, run unprivileged.
FROM python:3.15.2-slim-trixie@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5
COPY gateway/vault/requirements.txt /tmp/vault-requirements.txt
RUN pip install --no-cache-dir -r /tmp/vault-requirements.txt \
    && rm /tmp/vault-requirements.txt \
    && mkdir /data && chown 65532:65532 /data
COPY gateway/vault/vault.py /app/vault.py
USER 65532:65532
ENTRYPOINT ["python3", "-I", "-B", "/app/vault.py"]
