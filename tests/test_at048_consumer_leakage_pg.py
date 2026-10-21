"""AT-048 (S6 stage acceptance finding, scheduler 2026-10-21): the evaluation opening derives the Consumers of the cases itself.

A writer that groups the cases by Run can put one Consumer's Runs on both sides of the split; the opening returns leakage_detected
whatever the writer's case_groups say. Regrouped by Consumer the same cases open; the near-duplicate and case-group checks are
unchanged (their own tests in test_evolution_v1_pg). Reuses the E4/E5/V1 fixtures (synthetic message Runs of three Consumers)."""
import copy
import uuid

from nexloop_eios.evolution_store import diagnosis_id
from test_evolution_e4_pg import evo, table_digest  # noqa: F401
from test_evolution_e5_pg import SEED, proposed  # noqa: F401
from test_evolution_v1_pg import evaluation, status  # noqa: F401


def variant(f, label, design, validation, groups):
    d = f['drafts'][0]
    c = copy.deepcopy(d.candidate)
    c.update(candidate_id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'at048:' + label)), candidate_revision='sem-7+' + label,
             design_case_ids=sorted(design), validation_case_ids=sorted(validation))
    return type(d)(c, d.category, copy.deepcopy(d.patch), {r: groups[r] for r in design + validation}, d.regroups, d.source_consumer_ids)


def open_(f, v):
    f['store'].propose(diagnosis_id(f['diagnosis'].snapshot_digest), v)
    frozen = f['store'].freeze_bundle(v.candidate['candidate_id'], seed=SEED)
    return f['evaluator'].open(v.candidate['candidate_id'], frozen['bundle_digest'])


def test_one_consumer_on_both_sides_is_leakage_whatever_the_writer_grouped(evaluation):
    f = evaluation
    d = f['drafts'][0]
    runs = d.candidate['design_case_ids'] + d.candidate['validation_case_ids']
    consumer = dict(d.case_groups)  # the draft's clusters are the Consumers (consumer:<id>)
    v0 = d.candidate['validation_case_ids'][0]
    same = [r for r in runs if consumer[r] == consumer[v0] and r != v0]
    assert same, 'the fixture gives every Consumer two Runs'
    validation = sorted(set(d.candidate['validation_case_ids']) - set(same))
    design = sorted(set(runs) - set(validation))
    before = table_digest(f['admin'])
    by_run = variant(f, 'by-run', design, validation, {r: 'run:' + r for r in runs})
    refused = open_(f, by_run)
    assert refused == {'opened': False, 'candidate_id': by_run.candidate['candidate_id'], 'reason': 'leakage_detected', 'status': 'proposed'}
    assert status(f, by_run.candidate['candidate_id']) == 'proposed'
    # Regrouped by Consumer (every Run of v0's Consumer on one side): the opening passes.
    keep = sorted(set(runs) - set(same) - {v0})
    by_consumer = variant(f, 'by-consumer', sorted(set(keep) - set(validation)) or keep[:1], sorted(same + [v0]), consumer)
    opened = open_(f, by_consumer)
    assert opened['opened'] is True and opened['holdout_use'] == 1
    assert table_digest(f['admin']) == before  # AT-023: nothing formal written


def test_runs_without_a_consumer_do_not_match_each_other(evaluation):
    """A Run whose Consumer cannot be derived (null) is never "the same Consumer" as another such Run."""
    admin = evaluation['admin']
    assert admin.execute("select runtime.nexloop_evo_run_consumer(%s,'real',%s)", (evaluation['drafts'][0].candidate['tenant_id'], str(uuid.uuid4()))).fetchone()[0] is None
    d = evaluation['drafts'][0]
    run = d.candidate['design_case_ids'][0]
    assert admin.execute("select runtime.nexloop_evo_run_consumer(%s,'real',%s)", (d.candidate['tenant_id'], run)).fetchone()[0] == d.case_groups[run].removeprefix('consumer:')

