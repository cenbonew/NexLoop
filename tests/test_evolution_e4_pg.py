"""NX-038 E4 (design §3.1, §3.2, §11.3, §12.2, §12.5, §12.6; D15-D20): deterministic attribution, candidates and their event log,
three-tier evolution budget, erasure invalidation. AT-023 (isolation), AT-024 (structure permissions), AT-048 (no validation leak).

The pure rules run on synthetic samples and access records shaped like E2 and E3 output. The PostgreSQL part runs on a bootstrapped
catalog as the login role nexloop_evo_writer; admin only seeds synthetic Runs, the evolution budget row and erasures, and probes.
Nothing here holds a model, channel or effect credential.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from authority_fixture import seed_authority
from erasure_support import mark_erasing
from nexloop_eios.bootstrap import bootstrap
from nexloop_eios.evolution.attribution import attribute, draft_candidates, freeze_digest, split_cases
from nexloop_eios.evolution.candidate import Candidate
from nexloop_eios.evolution_store import CandidateStore, StoreUnavailable, diagnosis_id
from support.tenants import TENANT_A, TENANT_B
from test_evolution_traces_pg import hexid, private, run

NOW = datetime.now(UTC).replace(microsecond=0)
TERM = hashlib.sha256('synthetic-term'.encode()).hexdigest()
REF = {'ref': 'eios:object_type:Product', 'revision': '3'}
OTHER_REF = {'ref': 'nexloop:vocabulary:color/red', 'revision': '1'}


def sample(run_id, consumer, **over):
    base = {'schema_version': '1.0', 'source': 'database', 'run_id': run_id, 'interaction_id': None, 'run_kind': 'message', 'scenario': None,
            'issued_at': NOW.isoformat(), 'consumer_id': consumer, 'persona': 'real', 'test_stack': False, 'outcome': None, 'error_code': None,
            'model': {'calls': 1, 'failed': 0, 'unknown': 0}, 'tool_calls': 1, 'intents': {}, 'intent_ids': [], 'refusals': {},
            'failures': [{'code': 'run_failed'}], 'recovered_tool_errors': [], 'receipts': [], 'reconciliation': None}
    return {**base, **over}


def access(run_id, *, operation='browse', terms=(TERM,), refs=(), ambiguity=False, unresolved=0, kind='access', accessed=None):
    return {'access_id': str(uuid.uuid4()), 'kind': kind, 'run_id': run_id, 'context_id': None, 'accessed_id': accessed,
            'operation': operation if kind == 'access' else None, 'query_kind': 'any' if kind == 'access' else None,
            'term_hashes': list(terms) if kind == 'access' else [], 'coverage': (1.0 if refs else 0.0) if kind == 'access' else None, 'ambiguity': ambiguity if kind == 'access' else None,
            'result_refs': list(refs), 'result_count': len(refs), 'unresolved_count': unresolved, 'recorded_at': NOW.isoformat()}


def miss_case(run_id, consumer):
    """A failed Run whose browse found nothing while another read in it returned the definitions an alias could go on."""
    return sample(run_id, consumer), [access(run_id), access(run_id, terms=[hexid('other-' + run_id)], refs=[REF, OTHER_REF])]


# --- pure rules -------------------------------------------------------------------------------------------------------------------

def test_attribution_excludes_non_semantic_first_and_marks_closed_dimensions():
    r = [str(uuid.uuid4()) for _ in range(10)]
    c = [hexid('c' + str(i)) for i in range(4)]
    samples = [
        sample(r[0], c[0], test_stack=True),
        sample(r[1], c[0], refusals={'NXB01': 1}),                                   # budget refusal: never semantic
        sample(r[2], c[1], error_code='interaction_timeout'),
        sample(r[3], c[1], scenario='human_takeover', failures=[]),                  # no send: expected
        sample(r[4], c[2], scenario='restricted_after_refusal', intents={'delivered': 1}),  # a send: safety regression
        sample(r[5], c[2], failures=[{'code': 'run_failed', 'failure_tool': 'unregistered_tool'}]),
        sample(r[6], c[3]),
        sample(r[7], c[3]),
        sample(r[8], c[0], failures=[], outcome='ok'),                               # a miss in a successful Run is no failure signal
        sample(r[9], c[1], failures=[], recovered_tool_errors=[{'tool': 'lookup', 'code': 'bad_arg'}]),
    ]
    accesses = [access(r[1]), access(r[6]), access(r[7], operation='resolve', refs=[REF, OTHER_REF], ambiguity=True), access(r[8])]
    d = attribute(samples, accesses)
    got = {a.run_id: a.category for a in d.attributions}
    assert [got[x] for x in r] == ['filtered', 'not_semantic', 'not_semantic', 'expected_behavior', 'safety_regression', 'tool_misuse',
                                   'retrieval_miss', 'ambiguity_unresolved', 'none', 'none']
    assert d.marked == {'semantic_tool': 1} and d.weak_tool_signals == []  # one recovered error is below D13
    tool = next(a for a in d.attributions if a.category == 'tool_misuse')
    assert tool.dimension == 'semantic_tool' and tool.evidence['failure_tool'] == 'unregistered_tool' and tool.rationale
    # D13: the same (tool, code) at least 3 times from at least 2 Consumers is a weak semantic_tool signal; marked, never drafted.
    weak = [sample(str(uuid.uuid4()), c[i % 2], failures=[], recovered_tool_errors=[{'tool': 'lookup', 'code': 'bad_arg'}]) for i in range(3)]
    assert attribute(weak, []).weak_tool_signals == [{'tool': 'lookup', 'code': 'bad_arg', 'occurrences': 3, 'consumers': 2, 'dimension': 'semantic_tool'}]
    # Every output is ids, codes, counts, hashes and refs; the digest is reproducible from the same inputs (§2.3).
    assert d.snapshot_digest == attribute(list(reversed(samples)), list(reversed(accesses))).snapshot_digest == freeze_digest(samples, accesses)
    with pytest.raises(ValueError, match='2000'):
        attribute([sample(str(uuid.uuid4()), c[0])] * 2001, [])


def test_split_never_leaks_a_cluster_and_counts_regroups():
    """AT-048: a naive Run split that puts one Consumer on both sides is detected and regrouped."""
    clusters = {f'run-{i}': f'consumer:{i % 3}' for i in range(12)}
    seen_regroup = False
    for seed in range(40):
        design, validation, regroups = split_cases(clusters, f'seed-{seed}')
        assert design and validation and not set(design) & set(validation) and sorted(design + validation) == sorted(clusters)
        assert not {clusters[c] for c in design} & {clusters[c] for c in validation}
        seen_regroup |= regroups > 0
        assert (design, validation, regroups) == split_cases(clusters, f'seed-{seed}')  # deterministic
    assert seen_regroup
    with pytest.raises(ValueError):
        split_cases({'a': 'consumer:1', 'b': 'consumer:1'}, 's')


def test_drafts_only_for_the_open_dimension_above_the_bar():
    c = [hexid('d' + str(i)) for i in range(3)]
    samples, accesses = [], []
    for i in range(6):
        s, a = miss_case(str(uuid.uuid4()), c[i % 3])
        samples.append(s)
        accesses += a
    lone = sample(str(uuid.uuid4()), c[0])
    samples.append(lone)
    accesses.append(access(lone['run_id'], terms=[hexid('rare')]))  # one Run: below the bar
    d = attribute(samples, accesses)
    drafts, skipped = draft_candidates(d, samples, tenant_id=TENANT_A, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                       created_at='2026-10-20T00:00:00Z')
    assert len(drafts) == 1 and skipped == {'below_minimum': 1, 'no_scope': 0}
    draft = drafts[0]
    assert Candidate.from_contract(draft.candidate).status == 'proposed' and draft.candidate['dimension'] == 'semantic_content'
    assert draft.candidate['maximum_rounds'] == 3 and draft.candidate['allowed_change_scope'] == sorted([REF['ref'], OTHER_REF['ref']])
    assert draft.patch == {'change_kind': 'alias', 'target_refs': draft.candidate['allowed_change_scope'], 'term_hashes': [TERM],
                           'run_ids': sorted(s['run_id'] for s in samples[:6])}
    assert draft.source_consumer_ids == sorted(c)
    design, validation = draft.candidate['design_case_ids'], draft.candidate['validation_case_ids']
    assert not {draft.case_groups[x] for x in design} & {draft.case_groups[x] for x in validation}
    again, _ = draft_candidates(d, samples, tenant_id=TENANT_A, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                created_at='2026-10-20T00:00:00Z')
    assert again == drafts


# --- PostgreSQL ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def evo(admin, pg, tmp_path):
    bootstrap(admin)
    token, _ = seed_authority(admin, TENANT_A, 'eios:artifact:' + 'e' * 32)
    other_token, _ = seed_authority(admin, TENANT_B, 'eios:artifact:' + 'e' * 32, identity_suffix='-other')
    digest = hexid(token)
    admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','evolution','CNY',100,%s,%s,1)""", (TENANT_A, NOW - timedelta(days=1), NOW + timedelta(days=29)))
    consumers = [hexid('evo4-' + str(i)) for i in range(3)]
    samples, accesses = [], []
    for i in range(6):
        rid = run(admin, digest, NOW - timedelta(minutes=10 - i), consumer=consumers[i % 3], link='message')
        s, a = miss_case(rid, consumers[i % 3])
        samples.append(s)
        accesses += a
    connect = lambda: psycopg.connect(make_conninfo(pg, user='nexloop_evo_writer'), autocommit=True)  # noqa: E731
    credential = private(tmp_path / 'evo-writer', token)
    store = CandidateStore(connect, credential)
    other = CandidateStore(connect, private(tmp_path / 'evo-other', other_token))
    d = attribute(samples, accesses)
    drafts, _ = draft_candidates(d, samples, tenant_id=TENANT_A, parent_revision='sem-7', evaluator_version='l0-1', model_profile='none',
                                 created_at=NOW.isoformat().replace('+00:00', 'Z'))
    return dict(admin=admin, pg=pg, store=store, credential=credential, other=other, digest=digest, token=token, samples=samples, diagnosis=d, drafts=drafts,
                consumers=consumers, other_digest=hexid(other_token))


def opened(f):
    return f['store'].open_diagnosis(f['diagnosis'], f['samples'], parent_revision='sem-7', evaluator_version='l0-1', model_profile='none')


PROTECTED = ('ontology.object_type_versions', 'ontology.objects', 'ontology.nexloop_claims', 'ontology.nexloop_candidate_definitions',
             'control.nexloop_budget_limits', 'authz.nexloop_authority_facts', 'authz.nexloop_service_grants')


def table_digest(admin):
    out = {}
    for t in PROTECTED:
        if admin.execute('select to_regclass(%s)', (t,)).fetchone()[0]:
            out[t] = admin.execute(f'select md5(coalesce(string_agg(x::text,\'|\' order by x::text),\'\')) from {t} x').fetchone()[0]
    return out


def test_diagnosis_and_candidate_round_trip_isolated(evo):
    """AT-023: proposing writes only the candidate tables; the stored value is the evolution-candidate contract value."""
    f = evo
    admin = f['admin']
    before = table_digest(admin)
    out = opened(f)
    diag = diagnosis_id(f['diagnosis'].snapshot_digest)
    assert out == {'diagnosis_id': diag, 'status': 'open', 'deadline': out['deadline'], 'replayed': False}
    assert opened(f)['replayed'] is True
    proposed = f['store'].propose(diag, f['drafts'][0])
    wire = proposed['candidate']
    assert proposed['proposed'] is True and wire['status'] == 'proposed' and wire['publication_permit_ref'] is None
    assert wire['patch_artifact_ref'].startswith('evolution-patch:') and wire['tenant_id'] == TENANT_A
    assert {k: v for k, v in wire.items() if k not in ('patch_artifact_ref', 'created_at')} == \
        {k: v for k, v in f['drafts'][0].candidate.items() if k not in ('patch_artifact_ref', 'created_at')}
    assert f['store'].propose(diag, f['drafts'][0])['replayed'] is True
    read = f['store'].read(wire['candidate_id'])
    assert Candidate.from_contract(read['candidate']).to_contract() == wire and read['patch_present'] is True and read['cost_limit'] == '10'
    assert f['store'].close(diag)['status'] == 'closed'
    events = admin.execute('select event_kind,from_status,to_status from runtime.nexloop_evo_candidate_events order by event_id').fetchall()
    assert events == [('diagnosis_opened', None, 'open'), ('proposed', None, 'proposed'), ('diagnosis_closed', 'open', 'closed')]
    body = admin.execute('select body from runtime.nexloop_evo_patches').fetchone()[0]
    assert set(body) == {'change_kind', 'target_refs', 'term_hashes', 'run_ids'}
    assert table_digest(admin) == before  # no definition, object, Claim, budget limit, authority fact or grant changed
    # The other tenant sees nothing of it; NX-038 stops only proposed candidates.
    assert f['other'].read(wire['candidate_id']) is None
    assert f['store'].stop(wire['candidate_id'], 'user_interrupted')['status'] == 'incomplete'
    with pytest.raises(psycopg.Error, match='stops only proposed'):
        f['store'].stop(wire['candidate_id'], 'user_interrupted')
    for statement in ('delete from runtime.nexloop_evo_candidate_events', "update runtime.nexloop_evo_candidate_events set reason='x'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'), admin.transaction():
            admin.execute('set local role nexloop_owner')
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT_A,))
            admin.execute(statement)


def body_of(f, **over):
    draft = f['drafts'][0]
    body = {'diagnosis_id': diagnosis_id(f['diagnosis'].snapshot_digest), 'candidate': dict(draft.candidate), 'category': draft.category,
            'patch': dict(draft.patch), 'case_groups': dict(draft.case_groups), 'regroups': draft.regroups,
            'source_consumer_ids': draft.source_consumer_ids}
    for k, v in over.items():
        if k in ('candidate', 'patch'):
            body[k] = {**body[k], **v}
        else:
            body[k] = v
    return body


def propose_raw(f, body):
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_writer'), autocommit=True) as db:
        return db.execute('select authz.nexloop_evo_candidate_propose(%s,%s)', (f['digest'], json.dumps(body))).fetchone()[0]


def test_structure_permissions_and_leaks_are_refused(evo):
    """AT-024 and D19/D20 at the write function; AT-048 at freeze."""
    f = evo
    opened(f)
    fresh = lambda: {'candidate_id': str(uuid.uuid4())}  # noqa: E731
    with pytest.raises(psycopg.Error, match='dimension semantic_tool not open'):
        propose_raw(f, body_of(f, candidate={**fresh(), 'dimension': 'semantic_tool'}))
    # NX-040 D21: ontology_schema is open for proposal input only and narrowly (Object Type / Property scope, whitelist change
    # kinds; tests/test_nx040_schema_proposals_pg.py): this semantic draft's vocabulary scope is refused.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='change scope not allowed'):
        propose_raw(f, body_of(f, candidate={**fresh(), 'dimension': 'ontology_schema'}))
    for scope in ('eios:action:Grant.issue:1', 'eios:service_grant:x', 'authz:fact:role', 'nexloop:tool:send_message'):
        draft = f['drafts'][0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match='change scope not allowed'):
            propose_raw(f, body_of(f, candidate={**fresh(), 'allowed_change_scope': draft.candidate['allowed_change_scope'] + [scope]}))
    with pytest.raises(psycopg.Error, match='round budget over 3'):
        propose_raw(f, body_of(f, candidate={**fresh(), 'maximum_rounds': 4}))
    with pytest.raises(psycopg.Error, match='patch invalid'):
        propose_raw(f, body_of(f, candidate=fresh(), patch={'change_kind': 'action_definition'}))
    with pytest.raises(psycopg.Error, match='patch invalid'):
        propose_raw(f, {**body_of(f, candidate=fresh()), 'patch': {**f['drafts'][0].patch, 'text': 'free wording'}})
    # AT-048: one Consumer's Runs on both sides of the split are refused.
    draft = f['drafts'][0]
    design, validation = draft.candidate['design_case_ids'], draft.candidate['validation_case_ids']
    leak = {**draft.case_groups, validation[0]: draft.case_groups[design[0]]}
    with pytest.raises(psycopg.Error, match='validation leak'):
        propose_raw(f, body_of(f, candidate=fresh(), case_groups=leak))
    with pytest.raises(psycopg.Error, match='not the diagnosis Runs'):
        propose_raw(f, body_of(f, candidate={**fresh(), 'design_case_ids': design + [str(uuid.uuid4())]}))
    with pytest.raises(psycopg.Error, match='differs from its frozen diagnosis'):
        propose_raw(f, body_of(f, candidate={**fresh(), 'input_snapshot_digest': 'f' * 64}))
    assert f['admin'].execute('select count(*) from runtime.nexloop_evo_candidates').fetchone() == (0,)


def test_only_the_writer_role_with_a_service_credential_writes(evo):
    f = evo
    admin = f['admin']
    body = {'diagnosis_id': str(uuid.uuid4()), 'input_snapshot_digest': 'a' * 64, 'parent_revision': 'p', 'evaluator_version': 'e',
            'model_profile': 'm', 'run_ids': [f['samples'][0]['run_id']], 'consumer_ids': [], 'attribution': {}}
    for role in ('nexloop_evo_reader', 'nexloop_api', 'nexloop_domain_worker', 'nexloop_metrics'):
        assert admin.execute("select has_function_privilege(%s,'authz.nexloop_evo_diagnosis_open(text,jsonb)','execute')", (role,)).fetchone() == (False,)
        for table in ('nexloop_evo_candidates', 'nexloop_evo_patches', 'nexloop_evo_diagnoses', 'nexloop_evo_candidate_events'):
            assert admin.execute('select has_table_privilege(%s,%s,%s)', (role, 'runtime.' + table, 'select')).fetchone() == (False,)
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_writer'), autocommit=True) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('select * from runtime.nexloop_evo_candidates')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # the tenant comes from the credential: B cannot name A's Runs
            db.execute('select authz.nexloop_evo_diagnosis_open(%s,%s)', (f['other_digest'], json.dumps(body)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("select authz.nexloop_reserve_budget(%s,'real','evolution','x','1','CNY','evolution-candidate:'||gen_random_uuid())", (f['digest'],))
    with pytest.raises(StoreUnavailable):
        CandidateStore(lambda: psycopg.connect(make_conninfo(f['pg'], user='nexloop_evo_reader'), autocommit=True), f['credential']).read(str(uuid.uuid4()))


def test_three_tier_budget(evo):
    """§12.2 / D20: candidate 10, diagnosis 20 and 30 minutes, tenant month 100 (here lowered); evolution never counts as model."""
    f = evo
    admin, store = f['admin'], f['store']
    opened(f)
    diag = diagnosis_id(f['diagnosis'].snapshot_digest)
    cand = store.propose(diag, f['drafts'][0])['candidate']['candidate_id']
    assert store.reserve('candidate', cand, 'c-1', '6', 'CNY')['reserved'] is True
    assert store.reserve('candidate', cand, 'c-1', '6', 'CNY')['replayed'] is True
    spent = store.reserve('candidate', cand, 'c-2', '5', 'CNY')
    assert spent == {'reserved': False, 'reason': 'budget_exhausted', 'tier': 'candidate'}
    assert store.read(cand)['candidate']['status'] == 'incomplete' and store.read(cand)['end_reason'] == 'budget_exhausted'
    with pytest.raises(psycopg.Error, match='evolution candidate incomplete'):
        store.reserve('candidate', cand, 'c-3', '1', 'CNY')
    assert store.reserve('diagnosis', diag, 'd-1', '15', 'CNY')['reserved'] is True
    assert store.reserve('diagnosis', diag, 'd-2', '6', 'CNY')['tier'] == 'diagnosis'
    status = admin.execute("select status,end_reason from runtime.nexloop_evo_diagnoses").fetchone()
    assert status == ('incomplete', 'budget_exhausted')
    used = admin.execute("select budget_kind,sum(amount)::text from control.nexloop_budget_consumption group by 1").fetchall()
    assert used == [('evolution', '21')]  # nothing against `model`
    # Tenant month: a second diagnosis of another snapshot runs into the (lowered) monthly limit.
    admin.execute("update control.nexloop_budget_limits set limit_amount=25 where budget_kind='evolution'")
    second = attribute(f['samples'][:3], [])
    store.open_diagnosis(second, f['samples'][:3], parent_revision='sem-7', evaluator_version='l0-1', model_profile='none')
    second_id = diagnosis_id(second.snapshot_digest)
    assert store.reserve('diagnosis', second_id, 'd-3', '5', 'CNY') == {'reserved': False, 'reason': 'budget_exhausted', 'tier': 'tenant_month'}
    # Wall clock: a third diagnosis whose 30 minutes are over.
    third = attribute(f['samples'][3:], [])
    store.open_diagnosis(third, f['samples'][3:], parent_revision='sem-7', evaluator_version='l0-1', model_profile='none')
    with admin.transaction():
        admin.execute('set local session_replication_role=replica')
        admin.execute("update runtime.nexloop_evo_diagnoses set opened_at=opened_at-interval '1 hour',deadline=deadline-interval '1 hour' where diagnosis_id=%s",
                      (diagnosis_id(third.snapshot_digest),))
    assert store.reserve('diagnosis', diagnosis_id(third.snapshot_digest), 'd-4', '1', 'CNY')['tier'] == 'diagnosis_time'
    # Data separation: evolution consumption names an evolution source, and no other kind may.
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
            values(%s,'real','evolution','x-1',1,'CNY','model-call:1','p')""", (TENANT_A,))
    admin.execute("""insert into control.nexloop_budget_limits(tenant_id,world,budget_kind,unit,limit_amount,period_start,period_end,revision)
        values(%s,'real','model','CNY',100,%s,%s,1)""", (TENANT_A, NOW - timedelta(days=1), NOW + timedelta(days=29)))
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("""insert into control.nexloop_budget_consumption(tenant_id,world,budget_kind,consumption_id,amount,unit,source_ref,principal_id)
            values(%s,'real','model','x-2',1,'CNY',%s,'p')""", (TENANT_A, 'evolution-candidate:' + cand))
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("update control.nexloop_budget_limits set budget_kind='other' where budget_kind='model'")


def test_erasure_deletes_patches_and_invalidates(evo):
    """§12.5 / D17: a source Consumer's erasure deletes its patches; candidates (also accepted, unpublished ones) and diagnoses whose
    snapshot holds it end incomplete/missing_data; counts-only tombstone. An erased Consumer cannot enter a new snapshot."""
    f = evo
    admin, store = f['admin'], f['store']
    opened(f)
    diag = diagnosis_id(f['diagnosis'].snapshot_digest)
    cand = store.propose(diag, f['drafts'][0])['candidate']['candidate_id']
    admin.execute("select set_config('eios.tenant_id',%s,false)", (TENANT_A,))
    admin.execute("update runtime.nexloop_evo_candidates set status='accepted_for_release' where candidate_id=%s", (cand,))
    mark_erasing(admin, TENANT_A, f['consumers'][0])
    read = store.read(cand)
    assert read['candidate']['status'] == 'incomplete' and read['end_reason'] == 'missing_data' and read['patch_present'] is False
    assert admin.execute('select count(*) from runtime.nexloop_evo_patches').fetchone() == (0,)
    assert admin.execute('select count(*) from runtime.nexloop_evo_patch_sources').fetchone() == (0,)
    assert admin.execute('select status,end_reason from runtime.nexloop_evo_diagnoses').fetchone() == ('incomplete', 'missing_data')
    stone = admin.execute("select reason,item_class,subject_key,action,counts from runtime.nexloop_erasure_tombstones where item_class='evolution_candidate'").fetchall()
    assert stone == [('consumer_erasure', 'evolution_candidate', admin.execute('select runtime.nexloop_erasure_subject_key(%s,%s)',(TENANT_A,f['consumers'][0])).fetchone()[0], 'deleted',  # 0301: keyed
                      {'patches': 1, 'candidates_incomplete': 1, 'diagnoses_incomplete': 1})]
    kinds = admin.execute("select event_kind,from_status,to_status,reason from runtime.nexloop_evo_candidate_events where event_kind='invalidated' order by event_id").fetchall()
    assert kinds == [('invalidated', 'accepted_for_release', 'incomplete', 'missing_data'), ('invalidated', 'open', 'incomplete', 'missing_data')]
    with pytest.raises(psycopg.Error, match='erased Consumer'):
        again = attribute(f['samples'][:3], [])
        store.open_diagnosis(again, f['samples'][:3], parent_revision='sem-7', evaluator_version='l0-1', model_profile='none')


def test_without_an_evolution_budget_the_diagnosis_is_refused(evo):
    """NX-038 G1 positive control: a tenant without the evolution budget (the owner sets it up with the workbench loosen_budget and
    another owner's approval, NX-056 budget approval) cannot open a diagnosis, so every stage evolution check is blocked until it is set."""
    f = evo
    with f['admin'].transaction():  # the fixture's limit row is append-only: removed below the triggers, as a tenant that never set one
        f['admin'].execute('set local session_replication_role=replica')
        f['admin'].execute("delete from control.nexloop_budget_limits where budget_kind='evolution'")
    with pytest.raises(psycopg.Error, match='budget evolution not configured'):
        opened(f)
    assert f['admin'].execute('select count(*) from runtime.nexloop_evo_diagnoses').fetchone() == (0,)
