# NX-042 T3 — the consumer twin on the consumer port

Scope: design §5, D1, D4, D6, D7; builds on NX-041 S7 (port) and S7b (sim context), and on T1/T2. Migration `0280_nx042_twin_port.sql` (temporary number, contiguous after 0279; the dispatcher renumbers at merge). No contract change.

| Item | Implementation |
| --- | --- |
| Port entries | Three entries, executable only by `nexloop_twin_port` with the world's one credential (S7). Real and test are refused before any lookup (`authz.nexloop_sim_effect_tenant`).<br>• `authz.nexloop_twin_bind(digest, world, twin, version, seed)`<br>• `authz.nexloop_twin_observe(digest, world, binding, consumer, step, limit)`<br>• `authz.nexloop_twin_act(digest, world, binding, consumer, step)` |
| bind | Binds a published, not retired twin version with the template generator and a seed. A model generator is refused with `model_budget_required` (D6). `binding_id = sha256(world\|twin\|version\|seed)`; a repeat returns the binding. |
| observe | The S7b sim context pack, assembled by the one assembler `authz.nexloop_sim_context_command`, plus the observations offered to the Consumer (S7). Both are fixed by reference with their sha256 to (binding, consumer, step) in `runtime.nexloop_twin_observations`, as S7b fixes a Run's pack; a repeat returns the fixed observation. |
| act | The server decides the step from the twin version's decision layer (T2 `runtime.nexloop_twin_decide`, base `seed\|definition digest\|consumer`, instance = step). The first true behaviour by kind becomes one event: refuse_contact/unsubscribe, then payment, then reply/accept/message, then any other behaviour as visit. No true behaviour is a silent step with no event.<br>The event is written through the S7 entry `authz.nexloop_sim_consumer_act`, so the 100 000 event ceiling, replay and `step_reused` are unchanged. The step must be observed first (`step_not_observed`). The decision never reads the observation, so it reproduces byte for byte. |
| Provenance | The event's port version is `twin.<definition digest prefix>/<version>`. Its content attributes carry the twin id, version, seed, step, calibration status and `origin: twin`. `runtime.nexloop_twin_event_provenance` records per step the intervals and outcomes, the chosen behaviour, the twin id, version, digest, seed, the calibration status at that step (uncalibrated until T5) and the observation digest. |
| No credential in the twin | `nexloop_eios.twin_port.ConsumerTwin` is only (twin_id, version, seed). `TwinPort` holds the connection factory and the world's credential; it has no network client and calls no Action. |
| Tables | Bindings, observations and provenance: sim/shadow only, append-only (`sim_ledger_guard`), removed by the S2 whole deletion. |
| Redefinition | `authz.nexloop_sim_context_command`: the role check also admits a `nexloop_twin_port` session. The port role holds no execute on the assembler, so only the owner-run observe entry reaches it. Registered in `tests/test_function_copy_guard.py`. |
| NX-041 tests touched | `test_nx041_s7_pg.py`: the port role's reachable entries add the three twin entries. `test_nx041_host_sim_context_pg.py`: the source assertion allows exactly the T3 port-role clause besides app_role. |

Decision (dispatcher, 2026-10-21): the port role no longer executes the raw S7 entry `authz.nexloop_sim_consumer_act` (revoked in 0280); a twin's events go only through `authz.nexloop_twin_act` and always have provenance (`nexloop_twin_act` calls the raw entry as its owner). The S7 guard test asserts the port role cannot execute the raw act. The NX-041 tests that exercise the raw entry's own rules (validation, server time, ceiling, replay, backfill) or need the stub's exact content (S7b, sim context by reference, host sim context, whole deletion, frozen input) take the fixture `s7_raw`, which grants the raw act to the port role in that test's own database only.

Generators: bind checks the controlled generator list (T1) instead of the name.

Fix verify (L6 `nx042-t3-fix-verify.md`):
- 1.2: steps are contiguous and alternate per (binding, consumer). Observe n needs act n-1 (step 1 first); act n needs observe n. A skipped or earlier new step is refused (`step_out_of_order`); a repeated step replays. Steps start at 1, as the S7 port contract's `twin.step` (minimum 1) does. bind no longer returns the seed, the twin digest or the port version, and act's provenance no longer carries the digest, so a port client cannot compute a step's outcome ahead of it. The seed stays in the act provenance and in the event attributes: it is the client's own input and a required provenance mark.
- 4.2: the S7b assembler admits `nexloop_twin_port` for a sim:/shadow: world only (on top of `authz.nexloop_sim_effect_tenant`). The copy guard registration was updated.
- 5.4 (picking results across several seeds): NX-043 freezes an experiment's seed before it runs. Nothing in T3 stops a client from binding many seeds and comparing; the outputs are simulated and never selectable or facts. Left to NX-043.

Credential (owner 2026-10-21, extending D10 with a twin use): the port entries take only the world's `twin` credential (`authz.nexloop_twin_port_tenant`). The world's api, worker and agent credentials are refused with `twin port: credential_use`. The twin credential is revoked with the other three when the world is deleted. Migration `0284_nx042_twin_credential.sql`; details in `NX-041-D10-implementation.md`.

Tests: `tests/test_nx042_twin_port_pg.py`.
