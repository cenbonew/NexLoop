# Context contains an isolated compiled Host and frozen Pi production closure.
ARG PYTHON_IMAGE
ARG NODE_IMAGE
FROM ${PYTHON_IMAGE} AS python_runtime
FROM ${NODE_IMAGE}
# Security updates (NX-036): SECURITY_UPDATES is versions.lock.json images.node-host-base.security_updates, exact
# "package=version" pins of Debian fixes the Node base still lacks (the Node runtime itself stays the locked version). Only
# those packages are upgraded (--only-upgrade: nothing new is installed); apt verifies the signed Release metadata of the base
# image's sources, optionally through mirrors (APT_DEBIAN_MIRROR, APT_DEBIAN_SECURITY_MIRROR: http(s) base URLs; only host and
# path are replaced, as in deploy/postgres/Dockerfile). Every pinned package must end installed at exactly its pinned version.
ARG SECURITY_UPDATES
ARG APT_DEBIAN_MIRROR=
ARG APT_DEBIAN_SECURITY_MIRROR=
RUN set -eu; \
    test -n "${SECURITY_UPDATES}"; \
    mirror() { \
        case "$2" in http://*|https://*) ;; *) echo "mirror must be an http(s) URL" >&2; exit 1;; esac; \
        case "$2" in *[!A-Za-z0-9.:/_~-]*) echo "mirror URL has unexpected characters" >&2; exit 1;; esac; \
        files="$(grep -rlE "$1( |\$)" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true)"; \
        test -n "$files" || { echo "no apt source matches $1" >&2; exit 1; }; \
        for f in $files; do sed -E -i.nexloop-orig -e "s#$1 #$2 #g" -e "s#$1\$#$2#" "$f" && rm "$f.nexloop-orig"; done; \
    }; \
    if [ -n "${APT_DEBIAN_SECURITY_MIRROR}" ]; then mirror 'https?://deb\.debian\.org/debian-security' "${APT_DEBIAN_SECURITY_MIRROR%/}"; fi; \
    if [ -n "${APT_DEBIAN_MIRROR}" ]; then mirror 'https?://deb\.debian\.org/debian' "${APT_DEBIAN_MIRROR%/}"; fi; \
    apt-get update; \
    apt-get install --yes --no-install-recommends --only-upgrade ${SECURITY_UPDATES}; \
    for pin in ${SECURITY_UPDATES}; do \
        test "$(dpkg-query --showformat='${db:Status-Abbrev}${Version}' --show "${pin%%=*}")" = "ii ${pin#*=}" \
            || { echo "${pin%%=*} is not installed at ${pin#*=}" >&2; exit 1; }; \
    done; \
    rm -rf /var/lib/apt/lists/*
COPY --from=python_runtime /usr/local/ /usr/local/
# The Host runs only node and python3: the base image's npm and corepack (and their bundled packages) are removed (NX-036).
# CVE-2025-13836 (http.client read buffering) is fixed in CPython 3.12.13; the build fails on an older Python base (NX-036).
RUN ldconfig && python3 -c 'import sys; sys.exit(sys.version_info[:3] < (3, 12, 13))' \
 && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin nexloop \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
 && test ! -e /usr/local/lib/node_modules/npm && ! command -v npm && ! command -v corepack
COPY agent_host.py /opt/nexloop/scripts/agent_host.py
COPY runtime.tar /tmp/nexloop-runtime.tar
RUN mkdir -p /opt/nexloop/apps/agent-host && tar -xf /tmp/nexloop-runtime.tar -C /opt/nexloop/apps/agent-host && rm /tmp/nexloop-runtime.tar
COPY agent-host-main.js /opt/nexloop/apps/agent-host/dist/main.js
COPY package.json /opt/nexloop/apps/agent-host/package.json
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python3", "/opt/nexloop/scripts/agent_host.py"]
CMD ["--node", "/usr/local/bin/node", "--runtime-root", "/private/runtime", "--internal-key-file", "/private/host-server/control.key", "--port", "8100", "--tls-certificate-file", "/private/host-server/server.crt", "--tls-key-file", "/private/host-server/server.key"]
