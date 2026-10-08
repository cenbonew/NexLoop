# NX-018 Role Runtime boundaries

The implementation is integrated into the main repository as append-only migration `0063_role_runtime.sql`, following Context READ protection 0062. Existing published 0001..0062 checksums are retained. Main verification and first runner failures are recorded in `NX-018-core63-role-evidence.json`; NX-018 remains in progress.

## Actual path

`activate_role_plan` requires an existing genuinely issued Source Run and existing independently governed shared PlanStep/EffectPlan. It verifies current Source RoleDefinition, ConsumerRoleLink, PlanStep, ServiceOffering and ConsumerServiceOffering Object/Property READ; persists a Source-derived service-trigger event; creates a fsynced/hash-verified local Artifact; binds its v3 snapshot in PG; admits the queue event and registers the Run in the same transaction before ACK. RoleDefinition.ceiling_ref and ConsumerRoleLink.scope are responsibility metadata. This slice supports the actual current Source+Run EIOS authority as its ceiling; it does not compile arbitrary ceiling_ref policies or infer action authority from scope text. RoleDefinition and ConsumerRoleLink are responsibility metadata and grant no EIOS authority, queue rights, worker rights, or effect privileges.

Wire `nexloop.context-pack.v3` has exact `schema_version, bindings, formal_facts, current_constraints, supply, role_binding, trigger_statement`. `trigger_statement` is a persisted authenticated Source statement with service provenance, not a Human Message or formal fact. v1/v2 wire is unchanged; future relationship zone requires explicit v4. Host validates the protocol and actually passes the complete v3 input including responsibility to Pi. Runtime has no PG/provider credentials.

Generic Context Artifact READ uses the actual managed binding to select legacy or Role dependency. Legacy uses the latest separate Source READ guard including accepted Message body/actor equality. Role READ requires the current reader's own Object/Property READ for copied role/link/step/supply/formal refs and current Artifact READ. It never borrows producer token_digest. The first slice additionally requires the authenticated current reader to be the original Source principal owning the service-trigger body; independently authorized audit readers are not supported. A future independently governed SourceTrigger READ/retention Action is required for that extension.

## Recovery limit: not AT-004 full completion

Dispatch and finalization recheck the bound Role's current revision/activity/validity and actual Source current permissions. After provider acceptance, a governed `RoleMappingPort.end` denies new model/submit/dispatch/finalize. The existing currently authorized worker may QUERY the accepted external intent and commit its durable `observed_fulfilled` observation, but the original governed Action terminal receipt remains unfinalized. Repeated recovery does not issue another POST. This is retained evidence, not formal business completion.

Required next slice: a separate low-risk technical reconcile/finalize Action, authenticated by its own explicit current recovery authority, operating only on an already accepted stable intent plus validated current QUERY evidence and original fence/claim. It may register the known external receipt but cannot send a new POST, borrow a new Source role, or bypass a revoked Role through evergreen old proof. Parent decides and reviews that implementation; this candidate does not implement or claim it.

Two sequential actual short Pi Runs can share a PlanStep and businessIntent, producing one real loopback provider effect. Parallel Host exploration returned 503 under unchanged 2-second authorization timeout; parallel operational throughput remains unverified. Provider/model are owned deterministic test services, not live external channels/models. Binding after Run admission remains rejected, so the typed entry requires pre-issued Run/prepared planner intent and does not claim admission replay across that boundary.

## Integration

Main fixtures use catalog bootstrap rather than manual DRAFT SQL. Only owned source is committed; generated Node/Pi build outputs remain ignored. This checkpoint does not implement receipt reconciliation after Role revocation or relationship Context v4.

V2 Producer requires explicit configured control_id, compared against the actual existing governed EffectPlan. Snapshot/bind carry own Source Object READ for all four formal references and EffectControl Property READ for allow_effect/budget_units/executor_principal/valid_until, with current signed TTL checks before disclosure and after writes. Source grants must be independently supplied by a legitimate Configurator; Role never supplies them.
