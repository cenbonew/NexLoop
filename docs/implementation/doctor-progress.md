# Read-only foundation doctor

Changed files: nexloop_eios/doctor.py and tests/test_doctor.py. The module CLI uses NEXLOOP_DATABASE_URL and --artifact-root; DSNs are accepted only through private process configuration, not echoed in output. Empty DSNs never fall back to an unrelated default local database.

Doctor runs read-only PostgreSQL transactions to verify exact migration checksums and restricted application identity, checks an existing private Artifact directory without creating/writing/chmodding it, and returns only public model configuration. It performs no migrations, Artifact deletion, model call or production write. Exceptions become failed named checks without native secret/path/DSN contents. Foundation status is separate from product_ready, which remains false while the governed instance/effect/Pi recovery/Artifact write/Compose checks are unimplemented.

Commands: uv run --frozen pytest -q tests/test_doctor.py. Initial three tests passed in 2.17s. Review added the explicit missing-DSN refusal; final four cases test actual isolated PG with restricted app role, administrative-role rejection, absent/public Artifact directory refusal without mutation, and no default database connection. No test failed. This targeted result does not replace the prior complete CI evidence or claim community Compose startup.

versions.lock.json unchanged, bootstrap 0007. NX-006 remains in_progress; NX-009 remains not_started until its prerequisites are complete. No product AT is accepted. Full ActionGovernor assembly, business writes/effects, Pi Run recovery and community Compose remain actionable work; embedding/channel inputs limit only their corresponding later validation. No git staging/commit/push occurred.
