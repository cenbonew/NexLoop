# Build context must contain ONLY this file, the tested core wheel and frozen requirements.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
COPY requirements.txt /tmp/requirements.txt
# CVE-2025-13836 (http.client read buffering) is fixed in CPython 3.12.13; the build fails on an older base (NX-036).
RUN python -c 'import sys; sys.exit(sys.version_info[:3] < (3, 12, 13))' \
 && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin nexloop \
 && rm /tmp/requirements.txt
COPY nexloop_eios_core-0.1.0-py3-none-any.whl /tmp/wheels/nexloop_eios_core-0.1.0-py3-none-any.whl
RUN python -m pip install --no-cache-dir --no-deps /tmp/wheels/nexloop_eios_core-0.1.0-py3-none-any.whl \
 && rm /tmp/wheels/nexloop_eios_core-0.1.0-py3-none-any.whl
COPY web /opt/nexloop/web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "nexloop_eios.container_entrypoint"]
