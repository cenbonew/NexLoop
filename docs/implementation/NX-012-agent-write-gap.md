# NX-012 authorization scope audit

Authenticated SERVICE/AGENT governed writes, current application/release/parent
ceilings, restricted PostgreSQL Action dispatch and negative/revocation tests
are implemented in migration0033 and authorization.py. Evidence:
agent-governed-write-progress.md and agent-governed-write-evidence.json.

The additional docs07 section4 requirement is now implemented through trusted
AuthenticatedServices.issue_run_credential and Backend.authenticate_run in
migration0034. Server-generated Run UUID, fixed audience, exact world and
authorized published Action allowlist are persisted before returning the token;
1–300 second expiry uses the PostgreSQL clock. Source identity, current grants,
policy revisions and live parent ceilings are rechecked. No caller identity
override, nested minting or raw business SQL fallback is available. See
run-credential-progress.md for actual focused tests and CI status.

Final current-file CI944 Python/24 SQLite and fresh clean0034 Compose
verification passed. This closes the backend authorization-port gap. Host HTTP Run admission/tool proxy and
actual Pi recovery remain NX-014; durable Inbox/Outbox and lease/fence remain
NX-013; real-effect reconciliation remains NX-015–017. No complete S2 or
production deployment claim.
