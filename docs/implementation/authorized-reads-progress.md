# Service object and property READ checkpoint

NX-006/NX-008 progress; the complete NX-012/S2 capability matrix remains unaccepted.

## Real implementation and explicit source adaptation

New migration 0011_authorized_object_read.sql verifies a distinct trusted-backend signature, live credential/tenant/world/epoch and twelve actual EIOS authority fact snapshots for object READ, then selects the original ontology.objects row under FORCE RLS and an explicit world predicate. Requested properties each need their own exact property-target READ proof; SQL rechecks those proofs before projecting values. Default fields is empty and returns metadata only. Unsupported/missing obligations or field authority fail closed; no implicit all-properties read, browser session or raw application table SELECT is used.

object_reads.py uses the actual frozen AuthorizationFactsResolver and AuthorizationDecisionService for both ResourceType.OBJECT and ResourceType.PROPERTY. It binds exact canonical IDs and projects only named fields. This is an explicit service adaptation: the older PropertySecurityProjector requires browser-session authority, so no fake session is manufactured and no Memory verifier substituted. Redaction/masking variants and field writes remain unassembled; currently all requested fields must be authorized, otherwise the read fails.

The initial test exposed a real upstream split: ResourceType already has object/property/relation, while application restrictions and scope vocabulary exclude them. applications.py and grants.py now add these three exact types to the NexLoop fork vocabulary. Actual canonical-prefix validation, application ceiling, resource grants, scope catalog, clearance/control, policy intersection and freshness remain enforced. PROVENANCE.json retains the original upstream hashes and records the adaptation/new hashes; the upstream vocabulary test now expects the exact original tuple plus these three additions. Targeted source import/hash and application regressions passed. No unrestricted authority or break-glass expansion was introduced.

Tests/authority_fixture.py adds distinct service identity suffixes for synthetic test reader credentials. The combined object/property fixture persists genuine typed records and server credential/app ceilings for both scopes, then exercises real PG and actual EIOS decisions. A real governed Consumer create with declared string preference is read as metadata-only by default, then as the requested property after independent property grant checks. Revoking only the property grant leaves object metadata readable and rejects preference after reauthentication. Another actual revoke-after-evaluation/before-SQL race denies object read. Tenant B with its own same-text target grant cannot read tenant A's actual row.

Changed files: new SQL/catalog, object_reads.py; adapted applications.py/grants.py/provenance; authority and application tests; test_action_definitions.py; exact bootstrap/role/doctor expectations; versions.lock.json head 0010→0011. Old migrations 0001–0010 remain unchanged; upstream commits/image fields unchanged.

## Commands, first failure and reruns

uv run --frozen pytest -xq tests/test_bootstrap.py tests/test_action_definitions.py --tb=short: 18 passed in 11.73s before new reader cases.

First reader run failed synthetic authority setup: ResourceRestriction refused resource_type=object under its frozen vocabulary. Added the explicit NexLoop vocabulary extension and provenance, without weakening the permission intersection. `uv run --frozen pytest -xq tests/test_action_definitions.py tests/test_vendor_imports.py tests/upstream/test_eios_application_restrictions.py --tb=short`: 90 passed in 12.14s.

Added positive declared-property and property-only revoke integration: `uv run --frozen pytest -xq tests/test_action_definitions.py --tb=short`, 17 passed in 12.49s. Added tenant B row isolation before complete current-input CI, whose result follows below.

## Remaining scope

Preference mutation/expected-version fencing, Relation Actions, audit/events/outbox, authoritative schema/capability publish APIs, property masking/field write behavior, agent/run/release-bound credentials, API/Host/Compose, actual Pi Run recovery and S2 failure acceptance remain incomplete. No S2 AT is passed from these partial tests. No real model/channel credential was read or called, production DB touched, persistent environment migrated, or Git staging/commit/push performed. Fixture schema/authority setup uses admin only for configuration; business creation/read goes through restricted roles. Admin business queries are read-only evidence.

Final strict CI exit 0: 518 Python cases in 57.82s, 24 Pi SQLite FULL cases and four frozen-source builds, zero skips/errors/failures. Input/JUnit hashes in authorized-reads-ci.json. Prior migrations verified unchanged.
