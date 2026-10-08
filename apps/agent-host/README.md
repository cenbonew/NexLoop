# Agent Host 与受控 Pi RuntimeAdapter (NX-011 / NX-014)

This Node24/TypeScript process provides loopback HTTPS liveness and an internally authenticated readiness probe. It acquires an exclusive OS owner lock before listening. An explicit private runtime configuration enables governed Run start/resume/inspect/cancel through the independent Python Worker guard. It has no PostgreSQL DSN, database/admin/channel credentials, production shell tool or model call.

Build with `pnpm --filter @nexloop/agent-host build`. The supported entrypoint is the repository launcher:

```sh
uv run --frozen python scripts/agent_host.py \
  --node "$(command -v node)" \
  --runtime-root /absolute/private/local-runtime-directory \
  --internal-key-file /absolute/private/host-control-key \
  --port 8100 \
  --tls-certificate-file /absolute/private/host-certificate.pem \
  --tls-key-file /absolute/private/host-tls-key.pem
```

Use Node24. TLS certificate and key files are mandatory, service-owned mode0600 regular files bounded to32768 bytes; no HTTP listener or TLS fallback is provided. The certificate must cover the configured loopback IP SAN. The caller must prepare a dedicated existing local mode0700 directory and a service-owned mode0600 file containing exactly64 lowercase hexadecimal characters of randomly generated internal control key material. Never reuse a model credential, service business token or bootstrap secret. No secret is taken as a command-line value. This entrypoint inherits only PATH/LANG/LC_ALL/TMPDIR; provider integration remains separate work. The launcher does not create/delete a runtime directory or enumerate/recover Run files.

The Python launcher obtains a nonblocking `fcntl.flock` on `.nexloop-owner.lock`, sets its descriptor inheritable and replaces itself with Node via `execve`. Node directly holds this descriptor for its lifetime. There is no helper lock-holder process, PID-based ownership, heartbeat expiry or unlink-on-exit. The lock file remains on disk; its existence does not mean a live owner. A second Host refuses startup without acquiring it. SIGKILL releases the kernel lock; reopening preserves the same inode and existing runtime files. Only the supported launcher establishes ownership; directly running dist/main.js is not a supported service entrypoint.

`GET /health/live` is anonymous liveness. `GET /internal/v1/health/ready` requires `Authorization: Bearer <internal-control-key>`, exact loopback Host and no browser Origin/Sec-Fetch-Site. Current probes recheck directory/descriptor identity and permissions plus the private credential file, allowing operator-owned file replacement to rotate the internal key. Responses/logs do not reveal keys or request headers. Binding is only127.0.0.1, without proxy/header identity or CORS. This does not establish production transport or authorize an agent/service to perform EIOS Actions.

Readiness remains503 with `product_ready:false`: the current deterministic runtime profile does not prove business Action completion or production model/channel readiness. Actual local PG/HTTPS/Pi tests prove Run admission, SQLite WAL/FULL, single owner and kill/reopen of the same submission/task. Production mount and business reconciliation requirements remain separately unverified.

The Python API accepts the complete optional `--host-origin https://127.0.0.1:8100 --host-control-key-file /absolute/private/api-control-key --host-ca-file /absolute/private/trusted-host-ca.pem` group in addition to its required core arguments. API-side control key and CA files are private service-owned inputs. It performs a bounded readonly HTTPS probe of the fixed Host route with CA/hostname validation, no ambient proxy and no redirects. `/health/ready` includes a sanitized `host` report distinguishing reachability, authentication and owner-lock checks; product readiness staysfalse. Host key rotation may require replacing the API-side key copy. The probe supplies no business identity. Run admission additionally requires the opaque PG activation reference and live complete EIOS Run/task authority.

## Explicit runtime configuration and Worker

Start the launcher with `--runtime-config-file /absolute/private/host/runtime.json` to enable the current `deterministic-test` profile. This service-owned mode0600 JSON file contains exactly `guard_url`, `guard_key_file`, `guard_ca_file`, and `runtime_profile`. The URL must be the private loopback HTTPS guard endpoint ending `/internal/v1/runtime/authorize`; the CA and key files are explicit private inputs. The key is transport authentication only. PostgreSQL credentials, EIOS service/Run bearer secrets and model/channel secrets remain outside the Host.

The enabled Host admits authenticated `POST /internal/v1/runs/start|resume|inspect|cancel`. Start/resume requires exactly `{activation_ref, command, input}`; inspect/cancel requires `{activation_ref, command}`. The command is server-bound and its runtime_profile must be `deterministic-test`. Every operation uses the current EIOS activation guard; actual model/tool execution commits a monotonic PG execution-authorization marker before proceeding. Existing completed submissions replay the same receipt. Missing storage can be initialized only with a fresh explicit false marker and no orphaned WAL/SHM or inconsistent durable history. Existing execution authorization plus missing storage remains `runtime_state_missing`.

Use the packaged `nexloop-runtime-worker` entrypoint for ongoing queue polling and the guard listener. See [private Worker operation](../../docs/implementation/runtime-worker-operation.md). It stops subsequent claims on SIGTERM/SIGINT, settles the current bounded dispatch using its lease/fence, and releases its listener. After restart it claims expired PG work and reopens the original Run/request. No filesystem enumeration creates new Runs. Outcomes are explicitly runtime_only; business Action reconciliation remains a separate requirement.
