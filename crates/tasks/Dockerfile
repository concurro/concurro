FROM rust:1.100.0-slim-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p concurro-tasks

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory /src \
    && mkdir -p /src/crates/tasks \
    && useradd --system --uid 65533 --user-group --no-create-home --home-dir /nonexistent concurro
COPY --from=build /src/target/release/concurro-tasks /usr/local/bin/concurro-tasks
COPY --from=build /src/.git /src/.git
COPY --from=build /src/bundle /src/bundle
WORKDIR /src
USER 65533:65533
ENTRYPOINT ["/usr/local/bin/concurro-tasks"]
