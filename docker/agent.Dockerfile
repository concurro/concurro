# The agent image: the provider CLIs on a Debian base. Supply immutable digests and explicit CLI versions.
# BASE_IMAGE is the toolchain agents work with: Node alone by default; runners use the Rust image, so
# agents can build and test. Node and its global CLIs are copied in from NODE_IMAGE.
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
ARG BASE_IMAGE=${NODE_IMAGE}
FROM ${NODE_IMAGE} AS node

FROM ${BASE_IMAGE}
ARG CODEX_VERSION
ARG CLAUDE_VERSION
ARG COPILOT_VERSION
# Extra Debian packages the toolchain needs, such as clang for Rust crates with C dependencies.
ARG EXTRA_PACKAGES=""
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN test -n "$CODEX_VERSION" && test -n "$CLAUDE_VERSION" && test -n "$COPILOT_VERSION" \
    && ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates git $EXTRA_PACKAGES \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@openai/codex@$CODEX_VERSION" \
       "@anthropic-ai/claude-code@$CLAUDE_VERSION" "@github/copilot@$COPILOT_VERSION" \
    && npm cache clean --force \
    # Agents run unprivileged; a Rust toolchain stays usable, including toolchains a repository pins.
    && if [ -d /usr/local/rustup ]; then chmod -R a+rwX /usr/local/rustup /usr/local/cargo; fi
COPY engine/agent_entry.py engine/session_entry.py engine/bundle.py engine/policy.py engine/checks.py engine/repository.py /opt/concurro/engine/
COPY bundle /opt/concurro/bundle
ENV HOME=/tmp/home CODEX_HOME=/tmp/codex CLAUDE_CONFIG_DIR=/tmp/claude \
    COPILOT_HOME=/tmp/copilot COPILOT_AUTO_UPDATE=false
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python3", "/opt/concurro/engine/agent_entry.py"]
