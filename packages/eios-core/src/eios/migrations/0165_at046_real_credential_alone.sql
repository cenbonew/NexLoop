-- AT-046 (ROADMAP §10, v0.1 regression): a simulation world cannot borrow a real effect credential.
-- A service credential is world-scoped (0004: authz.nexloop_service_identity requires p_world=any(worlds)); a credential
-- listing 'real' together with any other world would let the same token, and every file and grant bound to its principal,
-- act in simulation, shadow or test as the real principal. A credential that holds 'real' now holds only 'real'. Every
-- existing writer (service_grants: [manifest world]; worker/sandbox profiles: one world) already complies.
-- NOT VALID first (new rows checked at once), then VALIDATE: a deployment holding a mixed credential fails here and the
-- migration rolls back; reissuing it per world is an owner decision.
-- Rollback: alter table authz.nexloop_service_credentials drop constraint nexloop_service_credentials_real_alone (new migration).
alter table authz.nexloop_service_credentials add constraint nexloop_service_credentials_real_alone
 check (not ('real'=any(worlds)) or worlds=array['real']::text[]) not valid;
alter table authz.nexloop_service_credentials validate constraint nexloop_service_credentials_real_alone;
