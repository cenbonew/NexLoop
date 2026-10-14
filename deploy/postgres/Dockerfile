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
ARG POSTGRES_IMAGE
FROM ${POSTGRES_IMAGE}
ARG PGBACKREST_VERSION
ARG PGBACKREST_DEB_VERSION
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
        apt-get update; \
        apt-get install --yes --no-install-recommends "pgbackrest=${PGBACKREST_DEB_VERSION}"; \
    fi; \
    test "$(dpkg-query --showformat='${Version}' --show pgbackrest)" = "${PGBACKREST_DEB_VERSION}"; \
    test "$(pgbackrest version)" = "pgBackRest ${PGBACKREST_VERSION}"; \
    rm -rf /var/lib/apt/lists/* /tmp/pgbackrest-debs; \
    install --directory --owner=postgres --group=postgres --mode=0750 /etc/pgbackrest /var/lib/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest
USER postgres
