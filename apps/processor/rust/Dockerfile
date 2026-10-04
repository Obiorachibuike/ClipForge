# ClipForge Rust processor sidecar.
#
# Multi-stage: compile with the full toolchain, ship a slim runtime that only
# needs FFmpeg. The binary is a pure orchestrator — it shells out to FFmpeg and
# FFprobe rather than linking libav, keeping the image small.
FROM rust:1.82-slim AS build

WORKDIR /src

# Cache the dependency build: copy manifests, compile a stub, then the sources.
COPY Cargo.toml ./
RUN mkdir -p src \
    && echo 'fn main() { println!("stub"); }' > src/main.rs \
    && cargo build --release \
    && rm -rf src

COPY src ./src
# `touch` forces a rebuild of the real sources after the stub replaced them.
RUN touch src/main.rs && cargo build --release


FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/target/release/clipforge-processor /usr/local/bin/clipforge-processor

RUN useradd --create-home --shell /usr/sbin/nologin processor
USER processor

ENV PROCESSOR_BIND=0.0.0.0:8100 \
    RUST_LOG=info

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/readyz >/dev/null || exit 1

CMD ["clipforge-processor", "serve", "0.0.0.0:8100"]
