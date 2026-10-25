# NX-058 slice D — implementation notes

Design: `docs/design/nx058-owner-actions.md` v2.5 (ADR-039 R1 §6, fdc06ef2). Developed on fc012e6e with temporary migration numbers; rebased onto main after batch 26.

## D1 — vote cooldown for every NX-056 policy (§2)

Migration `0339_vote_cooldown.sql` (temporary number):

- `control.nexloop_workbench_role_history.recorded_txid` (new column, default `txid_current()`, existing rows null): one transaction's rows are one step, so a handler's stop of one role and start of another (operator → owner) do not break a holding within the union.
- `control.nexloop_workbench_vote_eligible_from(tenant, world, subject, roles[])`: start of the subject's current continuous holding of any of the roles (all its principals, ordered by `recorded_at`, `entry_id`, judged per step) + 24 hours; null when nothing is held, for any world other than `real`, or without history (fail closed). Owner-held, granted to no role.
- `control.nexloop_approval_approver_eligible` (0326) additionally requires that time to have passed: decide refuses the vote (`approver_unauthorized`), valid_votes no longer counts it. Registered in the copy guard.
- Deployment: every configured active member without an open holding of its role gets a `start` (source `deployment`) at migration time.

**Runbook (deployment)**: for 24 hours after this migration no approval of any policy can be decided — including the first three (`real_dispatch.set` opening, `consumer.erase`, `retention.release_hold`). Choose the deployment time with the owner; during the window only the operations channel applies. A member who starts holding an approver role (joins, is promoted across the union, is resumed) votes 24 hours later.

Tests: `tests/test_vote_cooldown_pg.py` (the function's semantics; a vote within the cooldown refused; a voter who stopped holding the role no longer counted). NX-056 tests whose synthetic members vote at once record the holding as begun 25 hours ago (`tests/support/vote_cooldown.py`); the solo-mode test now shows a newly joined approver refused until ripe.
