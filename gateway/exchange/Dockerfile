# Concurro's key exchange: standard library Python plus openssl, run unprivileged.
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /data && chown 65532:65532 /data
COPY gateway/exchange/exchange.py /app/exchange.py
COPY engine/credential_routes.py /app/credential_routes.py
USER 65532:65532
ENTRYPOINT ["python3", "-I", "/app/exchange.py"]
CMD ["serve"]
