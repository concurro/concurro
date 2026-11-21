FROM rust:1.100.0-slim-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p concurro-accounts

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65533 --user-group --no-create-home --home-dir /nonexistent concurro
COPY --from=build /src/target/release/accounts /usr/local/bin/accounts
USER 65533:65533
ENTRYPOINT ["/usr/local/bin/accounts"]
