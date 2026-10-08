# NX-011 protected Session revocation

Migration 0029 and the private PG UnitOfWork implement the frozen RevokeBrowserSessionCommand. A dedicated identity operator locks the configured tenant/App Session exclusively, holds canonical App/Subject/account/membership authority rows, then revalidates active current Session. Revision and revocation time must match the active session: no stale revision, backward/future timestamp or repeated revoke. Only revoked_at and revision+1 change. Strict reply decoding/comparison occurs inside the transaction, so invalid replies roll back. No standalone SQL access is granted to the operator role.

The actual frozen BrowserSessionService.revoke checks current Session-bound CSRF before dispatch. This is private trusted composition; no client-controlled Session-ID revocation endpoint is enabled. The HTTP flow must derive current identity server-side and use this service with current credentials; the repository command itself is a trusted operator port, not a public CSRF proof.

Actual focused command: `uv run --frozen pytest -xq tests/test_browser_session_revoke.py tests/test_browser_session_touch.py --tb=short`: 5 passed in 4.19s, no first failure. Real PG confirms wrong CSRF leaves Session unchanged, correct logout persists revoked_at/revision2 and rejects the old token, future/stale commands fail, and four concurrent revokes produce exactly one successful mutation/revision increment. Existing touch/CSRF/expiry cases also ran.

Full local CI: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`; final result/hashes in browser-session-revoke-ci.json. versions.lock.json bootstrap revision 0028 -> 0029 only; prior28 SQL/dependency/upstream/image locks unchanged.

Rotation/replacement, server realm/principal resolution, HTTP login/logout cookie/CSRF flow, frontend/Host and S2/Pi full-loop evidence remain incomplete. NX-011 remains in_progress. No product acceptance promoted, persistent migration, server/original-system mutation, real model/channel call, staging/commit/push or deployment.
