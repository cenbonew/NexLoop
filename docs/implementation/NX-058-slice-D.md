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

Migration `0346_owner_member_actions.sql` (temporary number), on the batch 26+27 rehearsal ecc926f9:

- Registered: `workbench_member.promote_owner` (`promote_workbench_member`), `demote_owner` (`demote_workbench_owner`), `suspend_owner`, `resume_owner`, `revoke_owner` (`*_workbench_owner`), all `human`, one handler `control.nexloop_governed_change_workbench_owner`.
- Fail closed, two layers (design §1): the current policy must be exactly the owner-only shape, else `approval_unavailable`; the approval being executed (`nexloop.approval_in_flight`) must be this capability's, approved and unconsumed, this requester's, under the policy version just read, with this body's digest.
- Handler (design §3): requester an active owner; target only the body's principal; role and status rechecked under the lock; last active owner protected; the authority mark after every check; slice C's write order; sessions, reset codes and (for an owner no longer active as owner) its unused invitations end; audit carries the approval id.
- `control.nexloop_approval_open`: the body's `operation` must be the capability's registered operation (`operation_mismatch`); its target check also accepts a **suspended** member (resume_owner, demoting a suspended owner), a revoked one stays refused. Copy guard. **NX-056 change: needs L3's agreement.**
- `control.nexloop_approval_summary_allowed`: `demote_owner` shows `new_role`. Copy guard; `approval_policies.SUMMARY_ALLOWED` follows.
- `deploy/configuration/approval-policies.v1.json` is now version 2 (`expected_revision: 1`): version 1's three policies unchanged plus the five owner-only ones. The applied version 1 is kept as `deploy/configuration/history/approval-policies.version1.json` (a new database applies it first); the NX-056 tests that are about version 1 read that copy.
- Manifests: business-actions v47, workbench-roles v43 (owner 87 Actions), capabilities and catalog names +5. Counts: Actions 121.

Known gaps (next slice): `issue_owner_reset` and `invite_owner` — the workbench HTTP layer draws the code or token and puts its HMAC in the body, which becomes part of the approval digest, so the resubmission (a new code) never matches the approved request; needs an NX-056 decision (digest without those fields, or the code drawn at execution). A demoted owner's own waiting requests are not ended at once (the entry refuses executing them: the grant is gone); `requester_no_longer_eligible`, `quorum_unreachable` at open and the refusal reason codes wait for L3 (design D8). The last-owner check cannot fire through the handler today (the requester is itself an active owner other than the target); it stays as a guard.

Runbook order: publish the five Actions, then apply approval policies version 2 (dry-run, apply, compare sha256); before that the Actions answer `approval_unavailable`. NX-056 B1 must be live before enabling (design §0).

### D2 review follow-up (L6)

- `approval_open`: promote_owner never opens for a suspended member (the suspended exception is for the other owner-only Actions).
- Ending an owner's unused invitations writes one audit event per invitation (`nexloop.workbench_member.invitation_ended`, id and reason only).
- New tests: two approved suspensions of one owner executed on two connections (one executes, the other is refused after the lock or retries a transient error and is then refused); promotion ends the target's reset codes; a demoted active owner takes the new role's grants and history; a suspended owner's invitations end with audit; the last-owner count (active owners but the target).

## D3 — issue_owner_reset, invite_owner, acceptance v3

Migration `0348_owner_reset_and_invite.sql` (temporary number), on l2/nx058-owner-d3 from l3/nx056-b2 3ac3aa42 (0346 D2, 0347 B2):

- Registered with their approval settings in the same migration (ADR-039 R3 §8.3): `workbench_member.issue_owner_reset` (`issue_workbench_owner_reset`; execution fields `{code_hmac, issuer_source_hmac}`, targets `{active}`) and `workbench_member.invite_owner` (`invite_workbench_owner`; `{token_hmac, issuer_source_hmac}`, no target field).
- Handlers fail closed like D2 (`control.nexloop_workbench_owner_approval_v2`, which also takes the policy's target field - none for invite_owner), then follow slice B's issue and slice A's invite. Owner invitations record `approval_id`. The reset code is drawn at every submission, shown only when the submission executed the approved request, and the target's sessions end only then (R5).
- `authz.nexloop_workbench_invitee_free`: the workbench HTTP layer refuses an owner invitation of a username the tenant holds before any approval opens (ADR-039 §6.4); tenant from the session.
- `control.nexloop_accept_workbench_invitation_v3` (identity's entry; v2 revoked from it): an owner invitation needs its approval consumed as `executed`; an acceptance from the issuer's source writes `approval.same_person_flagged` (ids and the reason code only; counted by the v16 alert rule of B1).
- **L6 I-C2, option a**: the claim's request digest leaves out the capability's execution-time fields (`goal_controls.EXECUTION_FIELDS`, mirrored from the settings and tested), and `authz.nexloop_goal_governed_action` accepts the digest with or without them (canonical JSON). A retry of the execution with the same Idempotency-Key replays the claim (`replayed: true`, no second code). The HTTP layer also maps a remaining reservation conflict to 409 (option b as a safety net). Done before the policies are published.
- **L6 I-C1**: an owner demoted, suspended or revoked through the D2 handler loses its approved, unconsumed requests at once (`requester_no_longer_eligible`, B2's triggers); tested for all three, and the test fails with the triggers dropped.
- Approval policies version 3 (`expected_revision: 2`): the two policies added; version 2 kept as `deploy/configuration/history/approval-policies.version2.json`. Manifests: business-actions and workbench-roles +1 each (owner 89 Actions), Actions 123.

Runbook (lost response, R3 §8.4): a retry replays without the code or token; check "my requests" for `executed`, then revoke the code (the next issue voids it) or the invitation, and request again.

### D3 review follow-up (L6)

- **C-D3-1**: an owner's reset code redeems. `control.nexloop_workbench_reset_codes.approval_id` (new) records the approval issue_owner_reset ran under; `control.nexloop_complete_workbench_reset` (re-created in 0348, copy guard) decides the target's required standing by that origin - an active owner for an owner code, an active operator or reviewer otherwise - never by the target's role now. Tested end to end (redeemed once, new session epoch, login with the new password) and a code refused once its target is no owner.
- **I-D3-1**: acceptance v3's `executed` condition tested (an owner invitation tied to an approval consumed as `execution_refused` is refused, one tied to an executed approval accepted); removing the condition fails the test.
- **I-D3-2**: the D2 handler's member settle ends the target's unused codes (tested: a suspended owner's code ends, `member_changed`); its unaccepted invitations end on demotion and revocation as on suspension (tested).
- Replays of issue_owner_reset and invite_owner answer `secret: not_shown_again`, `next: revoke_and_request_again`. invite_owner's target statuses are not registered (it has no target field). Handler replay branches commented. I-C1 also asserts a re-promoted owner's old request never executes.
- Known: the same body by the same requester within an hour answers the earlier consumption (NX-056), so a second owner reset code for the same target waits an hour (the HMAC is outside the digest).
- Username case: slice D3 compares `lower(username)` like slice A; the approval view's `same_username_exists` (0342, B1) compares exactly - left to L3.
- **S-D3b-1**: `control.nexloop_approval_executed_for(tenant, approval, capability)`: an owner code redeems only on an executed `issue_owner_reset` approval, an owner invitation is accepted only on an executed `invite_owner` approval (tested: another capability's executed approval, an approved one never executed, one refused at execution).

## Slice E1 — staff offboarding erasure (0349)

See `docs/design/nx058-member-erasure.md` v2.4 and `docs/implementation/erasure-coverage.md` (NX-058 片 E). Migration `0349_member_erasure.sql` (temporary number) on the batch 28 rehearsal 01e08d0c: capability `workbench_member.erase_member` (target statuses `{revoked}`), the permanent erasure ledger, the deletion ledger kind and its replay, the shared erase write, the handler, the approval view's erased (ledger and the erased- invitee) and the request guard's invite_owner username rewrite (copy guard), the erased- username guard and the configuration guard; approval policies version 4 (version 3 kept as history). Manifests: Actions 124, owner 90. The 90-day login digest clearing (retention verb) is slice E2.

### E1 review follow-up (L6)

- Replay keeps the executor: `control.nexloop_deletion_ledger.actor` (new) is written with the erasure entry and carried into the restore journal (`control.nexloop_journal_on_deletion`, copy guard); the replay passes it, so a backup older than the erasure (no consumption) still gets the right `erased_by_principal`.
- Replay on a backup older than the revocation: `control.nexloop_workbench_member_revoke_for_replay` first revokes every membership of the subject, takes its principals out of the configuration, revokes their role grants and stops their holdings.
- The initial-account receipt's password fingerprint is overwritten by the erasure (a replay of the initial manifest then mismatches).
- The request guard's rewrite also requires a finished request and the staff erasure's own scope (`workbench-erasure:`).
- `sessions` counts deleted sessions (not token realm rows). New accounts with an `erased-` username are refused too (trusted configuration's initial accounts, acceptance).
- New tests: replay without a consumption record, replay before the revocation, target_is_requester, a waiting namesake invite_owner request left as is, the account-insert guard.

**Read-only check for stage before applying 0349** (the migration's own self-check; every column must be 0):

```sql
begin transaction read only;
select t.tenant_id,
  (select count(*) from control.nexloop_browser_accounts a where a.tenant_id=t.tenant_id and a.username like 'erased-%') as accounts,
  (select count(*) from control.nexloop_workbench_invitations i where i.tenant_id=t.tenant_id and i.username like 'erased-%') as invitations,
  (select count(*) from control.nexloop_approval_requests r where r.tenant_id=t.tenant_id
     and r.capability='workbench_member.invite_owner' and r.body->>'username' like 'erased-%') as invite_owner_bodies
from control.nexloop_tenants t order by t.tenant_id;
rollback;
```

Run it as a role that bypasses row-level security (these tables force RLS per tenant); with an ordinary owner role, `set local eios.tenant_id` per tenant first.
