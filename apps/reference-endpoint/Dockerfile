# Reference enterprise delivery endpoint: an external stand-in, not part of NexLoop. Standard library only.
# Build context: this directory only. PYTHON_IMAGE is a digest reference (versions.lock.json images.python-build-base).
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
RUN useradd --uid 10002 --user-group --no-create-home --shell /usr/sbin/nologin endpoint \
 && install -d -o 10002 -g 10002 -m 0700 /var/lib/reference-endpoint
COPY reference_endpoint.py /opt/reference-endpoint/reference_endpoint.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10002:10002
ENTRYPOINT ["python", "/opt/reference-endpoint/reference_endpoint.py"]
CMD ["serve", "--data-dir", "/var/lib/reference-endpoint", "--listen", "0.0.0.0:8443", \
     "--tls-cert-file", "/run/secrets/endpoint_cert", "--tls-key-file", "/run/secrets/endpoint_key", \
     "--service-token-file", "/run/secrets/service_token", "--staff-token-file", "/run/secrets/staff_token"]
