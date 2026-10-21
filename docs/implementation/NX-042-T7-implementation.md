# NX-042 T7 — the complete AT-046 acceptance with the twin, and the three acceptances of design §6

Scope: design §4, §5, §6 (the "proposed new acceptances" are in §6; §8 is the contract and migration proposal). No migration and no contract change. Tests: `tests/test_nx042_t7_acceptance_pg.py`. Planning acceptance entries are registered by the dispatcher.

| Acceptance | Test | Maps to | Status |
| --- | --- | --- | --- |
| AT-046 with the twin, end to end | `test_at046_the_twin_drives_a_complete_simulation_pi_run`: S3 Consumer, published twin version bound on the port (T3), 6 steps of observe/act, then the simulation group's actual Pi Run (NX-041 D10 chain, `run_sim_pi_run`: sim api issues the Run, registers the sim pack by reference, sim worker activates, sim guard plus simulation Host under Node 24 run it to `succeeded`). The Run's pack carries the twin's events. Every service credential used is of the world; the real tables (`real_counts`) and the real digest are unchanged. | AT-046; design §4 | passes (Node 24, serial) |
| §6 (a) twin output writes nothing to real | `test_acceptance_twin_output_writes_nothing_to_real`: bind, observe, act, and population sampling (T2). The real digest of every table is unchanged; a positive control shows that one real row changes it. | proposed (design §6); M20 | passes |
| §6 (b) calibration independence | `test_acceptance_calibration_windows_are_independent_and_grouped_by_consumer`: strict xfail until T5's calibration run exists. It flips to XPASS, which fails CI, when `runtime.nexloop_twin_calibration_run` appears. | proposed (design §6); AT-048 regression | **blocked on T4/T5** |
| §6 (c) no disguised probability | `test_acceptance_no_disguised_probability`: every output of an uncalibrated twin (run, samples, port events, provenance) says `uncalibrated` and gives intervals at least 0.01 wide, never a probability. The version is not selectable. A point definition is refused at publication. | proposed (design §6); D4, D7 | passes for what exists. The report's own mark is T5's; NX-043's refusal is NX-043's. |
| §5 facts vs simulated outputs | `test_twin_events_and_calibration_conclusions_are_never_facts_or_claims`: twin events reach the model's context as `simulated_input` from a `simulated` source. A passed calibration event adds no Claim, no object and no Commitment evidence in any world, and leaves the hypotheses `kind: hypothesis`. Commitment evidence refuses a sim world. A Claim needs a conversation, and a conversation is real only (CHECK), so no Claim can exist in a sim world. | design §5 | passes |

The real digest (`real_digest`) covers every base table of `authz`, `control`, `runtime`, `ontology` and `public`:
- a table with a world column, by its real rows;
- a durable-core table without a world column but with a job_id (`runtime.invocations`, `runtime.job_events`), by its job's world in `runtime.jobs`. The Pi Run's queue task writes there; its job is in the sim world;
- any other table, whole.

NX-041 test touched: `test_nx041_sim_pi_run_pg.py`.
- Steps 1–5 moved into `run_sim_pi_run(f, tmp_path)` so T7 reuses the chain. The original test keeps its checks.
- The forged pack of the registration negative now always differs from the fixed one. Before, it emptied `insufficient`, which a pack with events already has empty.

Open: D10 names three credential uses (api, worker, agent) and none for the twin port. The end-to-end test authenticates the port with the world's api credential. A dedicated `twin` use is the dispatcher's call.
