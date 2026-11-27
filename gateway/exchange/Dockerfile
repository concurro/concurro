# Concurro's key exchange: standard library Python plus openssl, run unprivileged.
FROM python:3.15.2-slim-trixie@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /data && chown 65532:65532 /data
COPY gateway/exchange/exchange.py gateway/exchange/inference.py /app/
COPY gateway/provider_protocol.py /app/provider_protocol.py
COPY engine/credential_routes.py /app/credential_routes.py
USER 65532:65532
ENTRYPOINT ["python3", "-I", "/app/exchange.py"]
CMD ["serve"]
