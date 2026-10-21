# NX-042 T2 — decision layer and seeded population sampling

Scope: design §2, §5, D1, D4, D6, D7. Migration `0276_nx042_twin_sampling.sql` (temporary number, contiguous after T1's 0275; the dispatcher renumbers at merge). Contract `nexloop-twin-sample/1` (`packages/contracts/twin-sample.schema.json`).

- `authz.nexloop_twin_sample(digest, world, twin, version, seed, count)`, executable only by `nexloop_sim_api` with the world's api credential (S6c app_role); real and test are refused through `authz.nexloop_sim_effect_tenant`.
- Decision layer `runtime.nexloop_twin_decide`: every behaviour returns the version's `low`/`high`/`coverage` and one boolean `simulated_outcome`. A probability is drawn inside the interval and the outcome against it, both from the S3 sha256 draw; the drawn probability is never stored or returned (D4).
- Reproducible: the draw base is `seed | definition_digest`, Consumers are the world's synthetic Consumers ordered by id and cycled, and `output_digest` covers what the samples say (not the world or the run id). The same seed and twin version give byte-identical samples in any world; a repeated call returns the same run.
- D6: only `twin-template/N` generators run; a model generator is refused with `model_budget_required` until a slice adds the model budget check.
- D7: runs and samples carry the twin's calibration status (uncalibrated until T5); NX-043 selects through `runtime.nexloop_consumer_twin_selectable` only.
- Storage: `runtime.nexloop_twin_sample_runs` and `runtime.nexloop_twin_samples`, sim/shadow worlds only, simulated output, append-only (`sim_ledger_guard`), removed by the S2 whole deletion. Nothing is written to Claims or ontology objects.
- `nexloop_sim_api` WHITELIST in `tests/test_nx041_sim_roles_pg.py` gains the new function. No existing function is redefined, so the copy guard is unchanged.

Tests: `tests/test_nx042_twin_sampling_pg.py`.
