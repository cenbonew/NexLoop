# NexLoop PostgreSQL image (nexloop/postgres): the pinned PostgreSQL 18 + pgvector image of versions.lock.json `images.postgres`
# with pgBackRest at the version of versions.lock.json `deployment_tools.pgbackrest` (NX-035: archive_command and the stanza
# run inside this container). Built only by scripts/release/release.py build (linux/amd64, base passed by digest, never pulled
# by tag), so the deployed image comes from a signed release. Nothing else is installed.
#
# Two package sources, chosen by the build context (scripts/prepare_postgres_build.py):
#   * default: the PGDG apt repository the base image already configures (apt verifies its signed Release metadata with the
#     key the base image ships); the package version is pinned exactly (PGBACKREST_DEB_VERSION);
#   * offline: debs/ holds pgbackrest_<PGBACKREST_DEB_VERSION>_amd64.deb and the dependencies the base image lacks, with a
#     SHA256SUMS file prepared and checked by the operator (NX-034-postgres-image.md); every file is checked here again and
#     installed with dpkg, without any repository access.
# Either way the build fails unless `pgbackrest version` is exactly "pgBackRest ${PGBACKREST_VERSION}".
#
# Optional package mirrors for the online path (APT_DEBIAN_MIRROR, APT_DEBIAN_SECURITY_MIRROR, APT_PGDG_MIRROR, each an http(s)
# base URL): only the host and path of the Debian, Debian security and PGDG sources are replaced; the signed-by keys and the
# suites stay those of the base image, so apt still verifies the signed Release metadata (and through it every package's
# sha256) exactly as with the origin servers. A mirror whose source line is not found fails the build.
ARG POSTGRES_IMAGE
FROM ${POSTGRES_IMAGE}
ARG PGBACKREST_VERSION
ARG PGBACKREST_DEB_VERSION
ARG APT_DEBIAN_MIRROR=
ARG APT_DEBIAN_SECURITY_MIRROR=
ARG APT_PGDG_MIRROR=
USER root
COPY debs/ /tmp/pgbackrest-debs/
RUN set -eu; \
    test -n "${PGBACKREST_VERSION}" && test -n "${PGBACKREST_DEB_VERSION}"; \
    if [ -f /tmp/pgbackrest-debs/SHA256SUMS ]; then \
        cd /tmp/pgbackrest-debs; \
        sha256sum --check --strict SHA256SUMS; \
        test "$(ls ./*.deb | wc -l)" -eq "$(grep -c '\.deb$' SHA256SUMS)"; \
        test "$(dpkg-deb --field "pgbackrest_${PGBACKREST_DEB_VERSION}_amd64.deb" Version)" = "${PGBACKREST_DEB_VERSION}"; \
        dpkg --install ./*.deb; \
        cd /; \
    else \
        mirror() { \
            case "$2" in http://*|https://*) ;; *) echo "mirror must be an http(s) URL" >&2; exit 1;; esac; \
            case "$2" in *[!A-Za-z0-9.:/_~-]*) echo "mirror URL has unexpected characters" >&2; exit 1;; esac; \
            files="$(grep -rlE "$1( |\$)" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true)"; \
            test -n "$files" || { echo "no apt source matches $1" >&2; exit 1; }; \
            for f in $files; do sed -E -i.nexloop-orig -e "s#$1 #$2 #g" -e "s#$1\$#$2#" "$f" && rm "$f.nexloop-orig"; done; \
        }; \
        if [ -n "${APT_DEBIAN_SECURITY_MIRROR}" ]; then mirror 'https?://deb\.debian\.org/debian-security' "${APT_DEBIAN_SECURITY_MIRROR%/}"; fi; \
        if [ -n "${APT_DEBIAN_MIRROR}" ]; then mirror 'https?://deb\.debian\.org/debian' "${APT_DEBIAN_MIRROR%/}"; fi; \
        if [ -n "${APT_PGDG_MIRROR}" ]; then mirror 'https?://apt\.postgresql\.org/pub/repos/apt/?' "${APT_PGDG_MIRROR%/}"; fi; \
        apt-get update; \
        apt-get install --yes --no-install-recommends "pgbackrest=${PGBACKREST_DEB_VERSION}"; \
    fi; \
    test "$(dpkg-query --showformat='${Version}' --show pgbackrest)" = "${PGBACKREST_DEB_VERSION}"; \
    test "$(pgbackrest version)" = "pgBackRest ${PGBACKREST_VERSION}"; \
    rm -rf /var/lib/apt/lists/* /tmp/pgbackrest-debs; \
    install --directory --owner=postgres --group=postgres --mode=0750 /etc/pgbackrest /var/lib/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest
USER postgres
