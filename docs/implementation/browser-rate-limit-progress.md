# NX-011 durable browser identity rate-limit port

Added append-only migration 0019_browser_identity_rate_limits.sql and browser_rate_limits.py implementing the actual frozen EIOS IdentityRateLimiter port. This is an authentication-request protection prerequisite, not a SessionCreationUnitOfWork substitute. The session contract still requires atomic canonical Subject/credential/membership/Application revalidation, evidence consumption and Session creation; no simplified session table is introduced and no login route is enabled.

The rate limiter uses configured tenant/action/trusted-operator policies, a server-keyed HMAC client digest, PostgreSQL's own clock, per-key transactional row locking and a counter retained across process/repository reopening. Raw client identifiers are not stored. The caller must derive canonical_origin/operator/tenant/client_identifier from trusted composition and transport, not browser tenant/forwarded-header claims. Counter/policy tables have FORCE RLS and no application read/write grants. Only nexloop_api can call the narrow protected function; direct table access, owner/admin impersonation and worker dispatch cannot stand in for the server ingress operator. Missing/revoked policy and invalid bindings fail closed with IdentityUnavailable, never Memory fallback.

Tests actually execute fresh independent PostgreSQL:

- Reopen preserves exhausted budget; the frozen IdentityRequestRateLimiter raises IdentityRateLimited.
- Twelve concurrent attempts allow exactly the configured two; counters remain bounded.
- Wrong tenant/operator and policy revocation fail without new counter rows.
- Direct application table reads/writes and admin/worker function calls are denied.
- Separate client HMACs have independent budgets; a real database-clock window expires and renews after an actual wait.

First targeted run: 5 passed/1 failed because head-update editing accidentally omitted 0018 from the expected ordered bootstrap list and left the expected count at 18. Corrected the explicit 0001..0019 sequence and count; no published SQL changed. `uv run --frozen pytest -xq tests/test_browser_rate_limits.py tests/test_bootstrap.py --tb=short` then passed 11 cases in 6.96s.

Full command: `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`; final evidence is browser-rate-limit-ci.json. All prior 18 migration SQL bytes were compared with the previous verified report and remain unchanged. versions.lock.json bootstrap_revision is now 0019; frozen sources, image statuses and dependency locks are unchanged. Current exact-head assertions and isolated installed-wheel smoke are updated.

NX-011 remains in_progress: actual PG browser Session UoW/account/evidence persistence, same-origin login/CSRF, frontend, Host/internal auth are incomplete. No browser/login AT, Docker runtime/AT-051, Pi recovery or S2 real-effect acceptance is claimed. Existing environments were not migrated; no server writes, real credentials/model calls, git staging/commit/push or deployment occurred.
