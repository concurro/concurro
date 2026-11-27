FROM rust:1.100.0-slim-trixie@sha256:3b199fd6db7d709274df56565b8f4003f9e30d7bfcff9d53148c86053b7855e2 AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p concurro-tasks

FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory /src \
    && mkdir -p /src/crates/tasks \
    && useradd --system --uid 65533 --user-group --no-create-home --home-dir /nonexistent concurro
COPY --from=build /src/target/release/concurro-tasks /usr/local/bin/concurro-tasks
COPY --from=build /src/.git /src/.git
COPY --from=build /src/bundle /src/bundle
ENV CONCURRO_TASKS_BUNDLE_DIR=/src/bundle
WORKDIR /src
USER 65533:65533
ENTRYPOINT ["/usr/local/bin/concurro-tasks"]
