# Supply an immutable Node image digest and explicit CLI versions when building.
ARG BASE_IMAGE=node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
FROM ${BASE_IMAGE}
ARG CODEX_VERSION
ARG CLAUDE_VERSION
ARG COPILOT_VERSION
RUN test -n "$CODEX_VERSION" && test -n "$CLAUDE_VERSION" && test -n "$COPILOT_VERSION" \
    && apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@openai/codex@$CODEX_VERSION" \
       "@anthropic-ai/claude-code@$CLAUDE_VERSION" "@github/copilot@$COPILOT_VERSION" \
    && npm cache clean --force
COPY engine/agent_entry.py engine/session_entry.py engine/bundle.py engine/policy.py engine/checks.py engine/repository.py /opt/concurro/engine/
COPY bundle /opt/concurro/bundle
ENV HOME=/tmp/home CODEX_HOME=/tmp/codex CLAUDE_CONFIG_DIR=/tmp/claude \
    COPILOT_HOME=/tmp/copilot COPILOT_AUTO_UPDATE=false
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python3", "/opt/concurro/engine/agent_entry.py"]
