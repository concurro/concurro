# The Concurro service: coordination only. With runner executors it runs no containers and no
# target code, so it needs no Docker access. Built from the host's clone; the image holds that
# commit's checkout, which the service serves and runner sessions must match.
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory /opt/concurro \
    && useradd --system --uid 65533 --user-group --no-create-home --home-dir /nonexistent concurro
COPY . /opt/concurro
# Only the commit itself: anything untracked in the build context is dropped, and the service
# refuses to start if a tracked file differs.
RUN git -C /opt/concurro clean -ffdxq && git -C /opt/concurro log -1 --format='Concurro %H'
ENV PYTHONDONTWRITEBYTECODE=1 GIT_OPTIONAL_LOCKS=0
WORKDIR /opt/concurro
USER 65533:65533
ENTRYPOINT ["python3", "-m", "service", "--settings", "/etc/concurro/service/settings.json"]
CMD ["serve", "--listen", "0.0.0.0:8787"]
