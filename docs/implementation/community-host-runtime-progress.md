# NX-009 Host container and NX-011 internal control integration

The actual Compose graph now runs PostgreSQL, API, Valkey and Agent Host plus three restricted bootstrap jobs and the check job. NX-009/NX-011 remain in_progress: the full community browser/Worker stack and S2 execution chain are outstanding.

## Implemented files

`deploy/community/Host.Dockerfile` and `scripts/prepare_host_build.py` build a separate five-file credential-free context from an actual locked Node24.13.0 TypeScript build. The Host image combines the pinned official Node/Python runtimes, launches through the existing OS-flock Python entrypoint and runs UID10001. It contains no EIOS wheel or business configuration.

`host_test_profile.py` initializes only fresh, empty private Host server/client/runtime volumes. Mode0700 roots and mode0600 files are service-owned. The server receives TLS cert/private key and control key; the API/check client receives only CA/control key. There is no database, Artifact signer, cache credential or model input in Host.

`container_entrypoint.py` connects the real API to the fixed HTTPS Host probe and adds anonymous/wrong-key/browser-Origin negatives. `compose.test.yaml` shares only the API network namespace with Host/check, publishes no ports, drops capabilities, uses readonly root/server config and waits for verified TLS liveness. `community_test.py` builds/verifies the separate context and refuses missing/failed actual Host or API-to-Host evidence. Tests, README, task evidence and Node image lock reflect this implementation.

## Commands actually executed

```sh
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen pytest -q tests/test_community_launcher.py tests/test_community_container_contract.py --tb=short
uv run --frozen python .ci-results/fetch_node_host_oci.py
docker image load --platform linux/arm64 --input .ci-results/node-host-verified-arm64.oci.tar
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py prepare --output .ci-results/community-host-runtime-20261008
source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen python scripts/community_test.py run --output .ci-results/community-host-runtime-20261008
uv run --frozen python .ci-results/verify_host_container_lifecycle.py
uv run --frozen python .ci-results/collect_host_runtime.py
source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check
```

The official Hub tag metadata and raw registry index were independently read through HTTPS; their SHA256 matched the locked digest. An explicit ignored OCI preload verified the original index, selected arm64 manifest/config/layers and retained the canonical multiarch RepoDigest. Standard image load was used; no daemon/proxy/TLS trust change or new registry credential. This is arm64 runtime evidence, not an amd64 run or a repair of daemon token fetching. Upstream image/runtime files retain their existing licenses; no upstream code history is copied.

## Failures and actual results

The initial focused tests had1 failure/22 passes: the negative mock accidentally used a tuple JSON key. Correcting this test data gave23 passes. No production implementation workaround was used. The fresh Compose launcher passed on its first attempt. Actual Node24.13.0 and Python3.12.10 were observed inside Host.

Evidence collection initially had a local syntax typo and then incorrectly treated tmpfs entries as Docker Mounts. The corrected collector verifies the two private volume destinations and the separate HostConfig.Tmpfs field explicitly. Its final assertions passed; no runtime resource was changed by those collector failures.

Actual API-to-Host TLS/control-auth/owner-lock checks passed; anonymous, wrong key and browser Origin were rejected. Runtime private key/control key were absent from container logs. Readonly inspection verified8 exact project/purpose-owned services, UID10001 runtime, split mounts, no host ports, internal network and no MODEL_ environment/admin input in Host.

For lifecycle verification, the real kernel refused a second flock and the supported second launcher exited1. After an ownership dry-run, only the disposable Host was SIGKILLed (exit137). The API observed Host unreachable while retaining readiness503. Restarting that exact container preserved the same owner inode/device/UID/mode and diagnostic file. Restarting the check job on the same PG/config reran governed Artifact/API/cache/Host checks successfully. This is not Pi Run recovery, SQLite FULL or PG lease fencing acceptance.

After the evidence was captured, ownership dry-run preceded stopping exactly Host/API/Valkey/PG. All stopped exit0; private inputs, runtime files, volumes and images were retained. No existing environment migration, original production change, commit, push or deployment.

Full CI exited0:887 Python tests in210.46s (one existing Starlette warning),24 Pi SQLite checks,four frozen Pi source builds and frontend/Host builds,zero critical skips. Dependency locks and all32 migrations were not edited. `versions.lock.json` only adds the official Node Host base index reference/digest and scoped verified build/runtime status; deployable Host image registry digest remains unresolved.

Remaining work: actual community frontend/browser bootstrap, Worker and derived two-host configuration, followed by the required governed service/agent writes, Pi RuntimeAdapter Run recovery and real effect/outbox/inbox chain. Model/channel validation remain separate. No additional input is needed for independent implementation.
