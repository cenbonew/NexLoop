# Context contains an isolated compiled Host and frozen Pi production closure.
ARG PYTHON_IMAGE
ARG NODE_IMAGE
FROM ${PYTHON_IMAGE} AS python_runtime
FROM ${NODE_IMAGE}
COPY --from=python_runtime /usr/local/ /usr/local/
RUN ldconfig && useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin nexloop
COPY agent_host.py /opt/nexloop/scripts/agent_host.py
COPY runtime.tar /tmp/nexloop-runtime.tar
RUN mkdir -p /opt/nexloop/apps/agent-host && tar -xf /tmp/nexloop-runtime.tar -C /opt/nexloop/apps/agent-host && rm /tmp/nexloop-runtime.tar
COPY agent-host-main.js /opt/nexloop/apps/agent-host/dist/main.js
COPY package.json /opt/nexloop/apps/agent-host/package.json
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python3", "/opt/nexloop/scripts/agent_host.py"]
CMD ["--node", "/usr/local/bin/node", "--runtime-root", "/private/runtime", "--internal-key-file", "/private/host-server/control.key", "--port", "8100", "--tls-certificate-file", "/private/host-server/server.crt", "--tls-key-file", "/private/host-server/server.key"]
