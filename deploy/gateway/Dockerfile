# NexLoop gateway image (nexloop/gateway): the locked official Caddy image (versions.lock.json images.gateway) with its caddy
# binary rebuilt from source (NX-036): the upstream release binary of the same Caddy version is built with a Go toolchain and a
# golang.org/x/net that carry fixed Critical findings, and no newer Caddy release exists yet. The build stage is the locked Go
# image (images.go-build-base, by digest); the module graph is fixed by caddy/go.mod and caddy/go.sum in this repository, so
# every downloaded module is checked against the committed hashes (a mismatch fails the build, whatever proxy serves it).
# GOPROXY_MIRROR optionally replaces the module proxy (an http(s) base URL; the go.sum check is unchanged). The binary keeps the
# official image's file capability (cap_net_bind_service) and location; nothing else in the image changes.
ARG GO_IMAGE
ARG CADDY_IMAGE
FROM ${GO_IMAGE} AS build
ARG GO_VERSION
ARG CADDY_VERSION
ARG GOPROXY_MIRROR=
WORKDIR /src
COPY caddy/go.mod caddy/go.sum caddy/main.go ./
RUN set -eu; \
    test -n "${GO_VERSION}" && test -n "${CADDY_VERSION}"; \
    test "$(go env GOVERSION)" = "go${GO_VERSION}"; \
    export GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0 GOPROXY="${GOPROXY_MIRROR:-https://proxy.golang.org}"; \
    go mod download && go mod verify; \
    test "$(go list -m -f '{{.Version}}' github.com/caddyserver/caddy/v2)" = "v${CADDY_VERSION}"; \
    go build -trimpath -ldflags '-s -w' -o /out/caddy .; \
    go version -m /out/caddy | grep -q "^/out/caddy: go${GO_VERSION}\$"
FROM ${CADDY_IMAGE}
ARG CADDY_VERSION
COPY --from=build /out/caddy /usr/bin/caddy
RUN set -eu; \
    setcap cap_net_bind_service=+ep /usr/bin/caddy; \
    getcap /usr/bin/caddy | grep -q 'cap_net_bind_service=ep'; \
    caddy version | grep -q "^v${CADDY_VERSION} "
