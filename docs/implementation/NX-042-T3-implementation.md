# NX-042 T3 — the consumer twin on the consumer port

Scope: design §5, D1, D4, D6, D7; builds on NX-041 S7 (port) and S7b (sim context), and on T1/T2. Migration `0277_nx042_twin_port.sql` (temporary number, contiguous after 0276; the dispatcher renumbers at merge). No contract change.

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

Open: the raw S7 entry `authz.nexloop_sim_consumer_act` stays open to `nexloop_twin_port` (the S7 `TwinStub` tests use it), so a port client can still write an event without twin provenance. Revoking it once nothing needs the stub is the dispatcher's call.

Tests: `tests/test_nx042_twin_port_pg.py`.
