FROM golang:1.24.8-bookworm AS builder

ARG MC_REF=RELEASE.2025-08-13T08-35-41Z

WORKDIR /src

RUN git clone \
    --depth 1 \
    --branch "${MC_REF}" \
    https://github.com/minio/mc.git \
    .

RUN CGO_ENABLED=0 \
    go build \
    -tags kqueue \
    -trimpath \
    -o /out/mc \
    .


FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /out/mc /usr/local/bin/mc

ENTRYPOINT ["/usr/local/bin/mc"]