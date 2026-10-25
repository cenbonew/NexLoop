# NX-058 slice D — implementation notes

Design: `docs/design/nx058-owner-actions.md` v2.5 (ADR-039 R1 §6, fdc06ef2). Developed on fc012e6e with temporary migration numbers; rebased onto main after batch 26.

## D1 — vote cooldown for every NX-056 policy (§2)

Migration `0343_vote_cooldown.sql` (temporary number):

- `control.nexloop_workbench_role_history.recorded_txid` (new column, default `txid_current()`, existing rows null): one transaction's rows are one step, so a handler's stop of one role and start of another (operator → owner) do not break a holding within the union.
- `control.nexloop_workbench_vote_eligible_from(tenant, world, subject, roles[])`: start of the subject's current continuous holding of any of the roles (all its principals, ordered by `recorded_at`, `entry_id`, judged per step) + 24 hours; null when nothing is held, for any world other than `real`, or without history (fail closed). Owner-held, granted to no role.
- `control.nexloop_approval_approver_eligible` (0326) additionally requires that time to have passed: decide refuses the vote (`approver_unauthorized`), valid_votes no longer counts it. Registered in the copy guard.
- Deployment: every configured active member without an open holding of its role gets a `start` (source `deployment`) at migration time.

**Runbook (deployment)**: for 24 hours after this migration no approval of any policy can be decided — including the first three (`real_dispatch.set` opening, `consumer.erase`, `retention.release_hold`). Choose the deployment time with the owner; during the window only the operations channel applies. A member who starts holding an approver role (joins, is promoted across the union, is resumed) votes 24 hours later.

Tests: `tests/test_vote_cooldown_pg.py` (the function's semantics; a vote within the cooldown refused; a voter who stopped holding the role no longer counted). NX-056 tests whose synthetic members vote at once record the holding as begun 25 hours ago (`tests/support/vote_cooldown.py`); the solo-mode test now shows a newly joined approver refused until ripe.

### D1 review follow-up (L6)

- The deployment start moved into `control.nexloop_workbench_role_history_backfill(source)` (owner-held, called once by 0343) and is tested on its own: configured active members without an open holding get one start; open holdings, suspended memberships and a second call write nothing.
- Worlds: approvals in simulation or shadow worlds count votes under the real-world cooldown (workbench members exist in real only).
- Known limitation: disabling and re-enabling a local account writes no role history, so it does not restart the clock (a disabled account cannot vote meanwhile); membership suspension and resumption do restart it.
- New tests: a member suspended and resumed by the slice C handler can neither approve nor reject for 24 hours, then approves and the requester's resubmission executes (end to end); an owner added by the configurator is refused until ripe.

## D2 — owner-only member Actions and approval policies v2

Migration `0344_owner_member_actions.sql` (temporary number), on the batch 26+27 rehearsal ecc926f9:

- Registered: `workbench_member.promote_owner` (`promote_workbench_member`), `demote_owner` (`demote_workbench_owner`), `suspend_owner`, `resume_owner`, `revoke_owner` (`*_workbench_owner`), all `human`, one handler `control.nexloop_governed_change_workbench_owner`.
- Fail closed, two layers (design §1): the current policy must be exactly the owner-only shape, else `approval_unavailable`; the approval being executed (`nexloop.approval_in_flight`) must be this capability's, approved and unconsumed, this requester's, under the policy version just read, with this body's digest.
- Handler (design §3): requester an active owner; target only the body's principal; role and status rechecked under the lock; last active owner protected; the authority mark after every check; slice C's write order; sessions, reset codes and (for an owner no longer active as owner) its unused invitations end; audit carries the approval id.
- `control.nexloop_approval_open`: the body's `operation` must be the capability's registered operation (`operation_mismatch`); its target check also accepts a **suspended** member (resume_owner, demoting a suspended owner), a revoked one stays refused. Copy guard. **NX-056 change: needs L3's agreement.**
- `control.nexloop_approval_summary_allowed`: `demote_owner` shows `new_role`. Copy guard; `approval_policies.SUMMARY_ALLOWED` follows.
- `deploy/configuration/approval-policies.v1.json` is now version 2 (`expected_revision: 1`): version 1's three policies unchanged plus the five owner-only ones. The applied version 1 is kept as `deploy/configuration/history/approval-policies.version1.json` (a new database applies it first); the NX-056 tests that are about version 1 read that copy.
- Manifests: business-actions v47, workbench-roles v43 (owner 87 Actions), capabilities and catalog names +5. Counts: Actions 121.

Known gaps (next slice): `issue_owner_reset` and `invite_owner` — the workbench HTTP layer draws the code or token and puts its HMAC in the body, which becomes part of the approval digest, so the resubmission (a new code) never matches the approved request; needs an NX-056 decision (digest without those fields, or the code drawn at execution). A demoted owner's own waiting requests are not ended at once (the entry refuses executing them: the grant is gone); `requester_no_longer_eligible`, `quorum_unreachable` at open and the refusal reason codes wait for L3 (design D8). The last-owner check cannot fire through the handler today (the requester is itself an active owner other than the target); it stays as a guard.

Runbook order: publish the five Actions, then apply approval policies version 2 (dry-run, apply, compare sha256); before that the Actions answer `approval_unavailable`. NX-056 B1 must be live before enabling (design §0).
