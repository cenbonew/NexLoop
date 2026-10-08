# Mandatory PostgreSQL and local Artifact backend composition

NX-006/NX-007/NX-008 progress. The public HTTP Host, RuntimeAdapter and complete S2 acceptance remain incomplete.

## Actual implementation

`nexloop_eios.backend.open_backend` now assembles the mandatory restricted PostgreSQL pool and the private fsync-backed LocalBlobStore. It loads an existing private owner-only signing file, verifies the application role and exact migration catalog before creating Artifact directories, and closes its owned filesystem descriptor and pool on exit. There is no migration, automatic credential/key provisioning or Memory fallback. Published migrations and versions.lock.json are unchanged at 0011.

`Backend.authenticate(token, world=...)` uses actual live service authentication, then returns an AuthenticatedServices facade with create_object, read_object, put_artifact, read_artifact and delete_expired_artifact. Tenant, principal and scopes come from the server identity. Creation uses the persisted Action/capability/schema bundle and actual ActionGovernor/claim port; reads use actual object/property EIOS facts; Artifact operations retain signed permits, metadata fencing and fsync behavior. The facade does not return the database pool or signing material. Individual operations still resolve live authority and SQL rechecks at dispatch.

A lifecycle RLock prevents shutdown from closing a pool or file descriptor during an operation. This first embedded backend is synchronous; concurrent API/worker dispatch and worker isolation remain future Host integration. Closed facades reject further calls with BackendClosed before touching resources. Python-private attributes are an internal convention, not a runtime sandbox or process isolation boundary.

A startup key-file bug was corrected: AuthoritySigner.from_file now opens with O_NONBLOCK as well as O_NOFOLLOW. It rejects non-regular files before reading, so a FIFO path no longer hangs waiting for a writer. Existing permission/ownership/32-byte validation is retained. PostgreSQL validates the configured signer on every protected request; opening the backend does not attest that its key was provisioned correctly, and does not imply product readiness.

## Real PostgreSQL evidence

Five tests in tests/test_backend.py cover: composed Artifact reserve/fsync/finalize/range-read and reopen; closed session handles plus actual descriptor/pool cleanup; denied raw business SELECT; normal service authentication rejection; actual published Consumer.create and stable-intent replay through the facade; object metadata read by a separate live service authority; administrative DSN rejection before directory creation; invalid signing-file permissions before DB connection/directory creation; and FIFO rejection without a writer. Test admin access provisions only synthetic authority/configuration and reads business row counts as evidence; actual business writes use nexloop_api and protected Actions.

## Commands and first failure

`uv run --frozen pytest -xq tests/test_backend.py --tb=short` first passed one test and failed setup of the create case because a second seed reused credential_id synthetic-a-key already installed by the published Action fixture. Corrected the synthetic identity suffix to -host, then added a distinct -host-reader identity for metadata READ. No production authority or runtime authorization was weakened.

The same targeted command then passed five tests in 2.46 seconds.

Full local CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Its final result and exact tested input/JUnit hashes are recorded in backend-composition-ci.json.

Changed files: packages/eios-core/src/nexloop_eios/backend.py (new); postgres_artifacts.py (nonblocking signing-file open); tests/test_backend.py (new); this report and CI report; live planning evidence; public change inventory.

## Example trusted-host assembly

```python
from pathlib import Path
from nexloop_eios.backend import open_backend

# Trusted Host configuration only: never expose these values to the Pi runtime.
with open_backend(database_url=restricted_dsn,
                  artifact_root=Path(private_artifact_directory),
                  signing_key_file=Path(private_key_file),
                  signing_key_id=configured_key_id) as backend:
    services = backend.authenticate(received_service_token, world="real")
    receipt = services.create_object(action_name="Consumer.create", action_version=1,
        intent_id=stable_intent_id, type_name="Consumer", properties={})
    # Send the receipt only after this method returns from its committed transaction.
```

The authenticated handle is valid only while the context is open, and revoked/changed directory snapshots can require reauthentication. No HTTP routes are provided yet. End-to-end workflow/event/audit/outbox, authoritative definition publication, preference mutation/relations, owner-bound Artifact GC, actual Pi Run kill/reopen, real effects, deployment and the complete AT matrix remain unfinished. No real model/channel credential was read or called, persistent environment migrated, original upstream checkout modified, or Git staging/commit/push performed.
