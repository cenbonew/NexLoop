# Non-editable core wheel independent-install checkpoint

NX-006 independent core packaging/startup evidence; complete community installation/Compose and S1/S2 gates remain incomplete.

## Observed packaging gap and actual repair

First actual command: `uv build --package nexloop-eios-core --wheel --offline --out-dir .ci-results/wheels` succeeded. Inspection found 122 wheel paths and 14 SQL files, but zero LICENSE/NOTICE/PROVENANCE entries. The original source workspace tests could not detect this distribution gap.

packages/eios-core now carries exact copies of the root LICENSE and NOTICE. pyproject.toml declares license-files for wheel metadata and force-includes PROVENANCE.json at eios/PROVENANCE.json. No license text or upstream hash is rewritten, no dependency added and no upstream history imported. The source package remains a vendored component, not a dependency on the original NEX-EIOS checkout.

## Actual isolated installation and PostgreSQL execution

New tests/test_wheel_install.py builds a non-editable wheel in a pytest-owned temporary directory, exports frozen non-dev third-party requirements excluding workspace projects, creates a separate Python 3.12 virtual environment and installs dependencies offline with require-hashes/no-deps, then installs only the built core wheel. No workspace editable path or original upstream path is installed in that environment. All subprocess waits are bounded and missing cache/dependencies fail rather than skip.

The archive test checks exact LICENSE/NOTICE bytes, the full provenance JSON, every recorded adapted source hash, every catalog SQL checksum and absence of domain package paths, .git, SQLite and compiled artifacts. The runtime subprocess uses python -I, a non-workspace cwd and asserts eios/bootstrap/smoke module paths live under the new virtual environment. It executes the wheel's bootstrap/verify against a fresh test-owned PG cluster, proving that packaged SQL/catalog resources are available. Then parent fixtures provision only synthetic test authority/key configuration, and the installed wheel independently performs actual restricted-PG test-world Artifact create/read through its CLI. Admin access only configures authority and reads evidence, never writes business objects. Original EIOS is neither imported nor connected.

Both generated console entry points are executed with --help in the isolated environment after clearing PYTHONPATH/PYTHONHOME. The Artifact smoke result excludes credential, DSN and signer bytes and persists exactly one available test-world record. .ci-results/wheel-install.json records the actual tested wheel hash and isolation/runtime checks. This is cached-dependency offline installation evidence, not proof that a new offline computer can obtain dependencies without a prior cache.

## Commands and results

`uv run --frozen pytest -xq tests/test_wheel_install.py --tb=short`: 2 passed in 3.07 seconds after the packaging repair. No pytest failure/skip occurred; the first wheel inspection, not a failed bootstrap, exposed the license/provenance omission. The complete current-input CI additionally tests installed console help and records the wheel hash.

Full CI command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`. Results, input/JUnit and tested wheel hashes are recorded in core-wheel-ci.json. A final local wheel rebuild is compared with the tested wheel hash before reporting it as the same artifact.

Changed files: packages/eios-core/LICENSE, NOTICE and pyproject.toml; tests/test_wheel_install.py; this report/CI evidence; planning evidence; public file inventory. Built wheels/virtual environments are ignored disposable test artifacts. versions.lock.json and dependency locks are unchanged, bootstrap stays 0014 and all prior migration SQL bytes are unchanged. Frozen upstreams and unresolved image digests are unchanged.

Community identity/key provisioning, Compose, runtime-owner Artifact lifecycle, full orphan worker, HTTP/Host, actual Pi Run kill/reopen, real effects/outbox and complete S2 acceptance remain incomplete. AT-051 is not passed solely from this packaged-component test. No actual deployment/real model validation is claimed. No original checkout/production DB was modified, persistent environment migrated, real credential read/called, or Git staging/commit/push performed.
