# EIOS write-path audit

Source commit: `1e363db982daeca74058453ad12fc4a6cac333da`. This report is source evidence, not proof that NexLoop currently has an agent write path.

## 0061: browser/session governance

Read `src/eios/migrations/0061_authorization_governance.sql` (464 lines), specifically `assert_live_governance_decision`, `issue_execution_permit`, `consume_execution_permit`, approval and obligation checks. A permit starts from an allow decision with every intersection true and an unexpired authoritative revision vector. Approval cannot change deny to allow. Subject, session, credential, application, agent-chain, target, operation and scope digests must exactly match. Issuance verifies the live session; expiry is bounded by the decision and five minutes. Consumption verifies the live decision/session and unchanged approval/obligation bundles, then atomically changes used_count from zero to one and records command/binding evidence. Governance audit chains have CAS-protected heads and a SIEM outbox. Later migrations strengthen these functions: copying 0061 alone is not the final frozen behavior.

API keys have no `control.sessions` row (0053 permits local_account/external_identity). Passing a service identity off as a human session cannot legitimately use this ABI.

## 0313: parallel API Key execution

Read `src/eios/migrations/0313_api_key_capability_execution.sql` (1052 lines), its full resolve/submit/live/claim/heartbeat/finish/schema-definer flow, and `src/eios/api/app.py`'s API-key dispatch branch. The immutable allowlist contains exactly:

- `ontology.schema.event_type.register@1.0.0`
- `ontology.schema.object_type.register@1.0.0`
- `ontology.schema.relation_type.register@1.0.0`

`api_key_live_identity` resolves an active key, tenant, active membership and subject, current credential revision, and exact application binding. Key expiry/revocation/deletion/retirement all matter. `api_key_capability_ownership` validates published active application and bound agent release, capability ownership, chain and scopes. Resolve returns credential_kind=api_key with null session fields. Submit consumes the decision once using unique decision_id rather than the session-bound permit ABI. It verifies identity/ownership, decision vectors, payload and runtime-semantic digests; replay of a command with altered payload conflicts.

API-key jobs live in a separate table, because the browser claimer rejects jobs without live sessions. At claim, `assert_api_key_capability_job_live` re-resolves identity and both bindings and verifies decision authority, revision digests and capability target. Stale queued authority becomes rejected. Each worker operation presents worker ID, token, fence and unexpired lease. uncertain is a terminal state, never automatically re-claimed. Worker SQL permissions expose lifecycle and schema definers, not direct table writes.

Schema writes may create a new name or increment a schema already owned by an API-key job. They cannot alter seed/collector/migration-owned schemas, protected inventory types, published-definition-pinned object types, allow_breaking, or only_edit_via_actions. `api_key_schema_head/register` derive target from the claimed payload and use the live job/claim.

## Service authority is not API Key write availability

Read `0167_service_principal_write_grants.sql` (91 lines). It gives the orchestrator service subject tennis-effect-writer and tenant-super-admin role grants plus internal clearance, without identity-admin. Despite its broad comment, the SQL explicitly filters `membership.subject_id='svc-orchestrator-subject'`. This is domain-specific seeding, not generic service onboarding. A service may possess delegable authority, but that does not broaden 0313's capability allowlist or grant an API Key an instance-write channel. NexLoop must exclude this production-specific seed.

## NX-012 extension points

1. Adapt the existing 0313 parallel authenticated service/agent flow to new independent lineage; preserve key/membership/application/release/role ceilings, scopes, resource grants, controls/policy intersection, authoritative decision audit, immutable payload binding and claim-time revalidation.
2. Add narrow Consumer object and preference/Relation capabilities with expected_version and stable intent IDs. Extend SQL constraints, resolver allowlist, worker dispatch and protected-definer paths together; widening only Python routing or only the allowlist is insufficient.
3. Bind run ID, audience, world, scope and validity to server-issued short-lived runtime credentials. Service identity remains service; human delegation stays a distinct requirement where needed.
4. Recheck authorization at dispatch and revoke queued work on key/application/release/grant/policy changes. Runtime gets no DB credentials. App and action worker have function-only write grants; no direct table INSERT/UPDATE/DELETE.
5. Test two tenants, wrong actor/world, ceiling/scope/property denial, missing authorization dependency, stale revision, revoke-after-plan and revoke-after-claim, fencing, command/payload conflict and restart recovery against real PostgreSQL.

No governed NexLoop instance-write success is claimed by this audit. NX-012 stays not_started until preceding bootstrap and API tasks pass.
