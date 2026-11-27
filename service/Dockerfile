# The Concurro service: coordination only. With runner executors it runs no containers and no
# target code, so it needs no Docker access. Built from a full clone; the image holds that
# commit's checkout, which the service serves and runner sessions must match.
FROM python:3.15.2-slim-trixie@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory /opt/concurro \
    && useradd --system --uid 65533 --user-group --no-create-home --home-dir /nonexistent concurro
COPY service/requirements.txt /tmp/concurro-requirements.txt
COPY contracts/python/requirements.txt /tmp/concurro-contracts-requirements.txt
RUN pip install --no-cache-dir -r /tmp/concurro-requirements.txt -r /tmp/concurro-contracts-requirements.txt
COPY . /opt/concurro
# Only the commit itself: anything untracked in the build context is dropped, and the service
# refuses to start if a tracked file differs.
RUN git -C /opt/concurro clean -ffdxq && git -C /opt/concurro log -1 --format='Concurro %H'
ENV PYTHONDONTWRITEBYTECODE=1 GIT_OPTIONAL_LOCKS=0 PYTHONPATH=/opt/concurro/contracts/python
WORKDIR /opt/concurro
USER 65533:65533
ENTRYPOINT ["python3", "-m", "service", "--settings", "/etc/concurro/service/settings.json"]
CMD ["serve", "--listen", "0.0.0.0:8787"]
