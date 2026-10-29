"""World-model glossary (migration 0374; docs/design/world-model-glossary.md r2): real PostgreSQL, the real workbench and the real
configurator. The v1 manifest (304 entries) is published from its exact text; a later manifest keeps what it leaves out, restores to
machine translation what it gives as null and clears by hand what it gives as ''. The owner corrects one gloss through
nexloop.world_glossary.set (operators refused, exact keys, tenant and world from the session, the identifier checked at dispatch). The
world-glossary-translator reaches PostgreSQL only through its signed port: a quota reservation before every model call (a full day
calls the model zero times), identifier metadata only in the model input, never over a manual gloss (also when a correction lands
between the call and its record), failures backed off and failed after three, tokens and cost recorded (unpriced without a tariff).
Synthetic tenants, members and model stand-ins only."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from commercial_fixture import publish_registry_actions
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from multi_authority_fixture import seed_multi_authority
from nexloop_eios.assembly import open_core
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable
from nexloop_eios.world_glossary import (CONTEXT_LIMIT,GlossaryPort,SYSTEM_PROMPT,WorldGlossaryTranslator,load_tariff,model_input)
from support.tenants import TENANT_A as TENANT
from test_real_dispatch_switch_pg import owner_sql
from test_workbench_actions_http import act
from test_workbench_read_pg import (browser_business, conversations, identity, provision, published_action, uow, workbench,  # noqa: F401
                                    workbench_login)

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / 'deploy/configuration/world-model-glossary.v1.json'
TARIFF = ROOT / 'deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json'
TRANSLATE = ('eios:action:nexloop.world_glossary.translate:1', ResourceType.ACTION, Operation.EXECUTE)
spec = importlib.util.spec_from_file_location('world_glossary_apply', ROOT / 'scripts/deploy/world_glossary_apply.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def publish(configurator, manifest, expected):
    @contextlib.contextmanager
    def window(path, refused):
        yield configurator
    out = io.StringIO()
    code = cli.main(['--manifest', str(manifest), '--tenant', TENANT, '--bootstrap-dsn-file', 'unused', '--expected-revision', str(expected)],
                    out=out, window=window)
    return code, json.loads(out.getvalue())


def glossed(client):
    response = client.get('/api/v1/workbench/world-model')
    assert response.status_code == 200, response.text
    return response.json()


def row(admin, kind, identifier):
    return admin.execute('select gloss,source,revision from control.nexloop_world_glossary where tenant_id=%s and kind=%s and identifier=%s',
                         (TENANT, kind, identifier)).fetchone()


@pytest.fixture
def glossary(workbench, tmp_path):
    w = workbench; admin = w['admin']
    publish_registry_actions(admin, ('nexloop.world_glossary.set',), tenant=TENANT)
    provision(w)
    return dict(w=w, admin=admin, tmp=tmp_path)


def test_v1_is_published_from_its_exact_text_and_the_world_model_carries_the_glosses(glossary):
    g = glossary; admin, w = g['admin'], g['w']
    digest = hashlib.sha256(V1.read_bytes()).hexdigest()
    code, out = publish(w['configurator'], V1, 1)
    assert code == 2 and 'expected revision 1, current 0' in out['reason']
    code, out = publish(w['configurator'], V1, 0)
    assert code == 0 and out['entries'] == 304 and out['manifest_sha256'] == digest and out['result']['manual_entries'] == 304
    manifest = json.loads(V1.read_text())
    assert len(manifest['actions']) == 129 and manifest['actions']['nexloop.world_glossary.set'] == '设定世界模型译名'
    assert admin.execute('select count(*) from control.nexloop_world_glossary where tenant_id=%s and source=%s', (TENANT, 'manual')).fetchone()[0] == 304
    code, again = publish(w['configurator'], V1, 0)  # a replay writes nothing
    assert code == 0 and again['result']['replay'] is True
    other = g['tmp'] / 'other.json'
    other.write_text(json.dumps({**manifest, 'decision': 'another text'}, ensure_ascii=False))
    code, out = publish(w['configurator'], other, 0)
    assert code == 2 and 'published with another text' in out['reason']
    with w['client']() as client:
        assert workbench_login(client, w['operator']).status_code == 200
        model = glossed(client)
    assert model['glossary']['manifest_version'] == 1 and model['glossary']['sections']['Object Type'] == '对象类型'
    consumer = next(t for t in model['object_types'] if t['name'] == 'Consumer')
    assert (consumer['gloss'], consumer['gloss_source']) == ('客户', 'manual')
    for relation in model['relation_types']:
        if relation['inverse_name'] == '':
            assert relation['inverse_gloss'] is None and relation['inverse_gloss_source'] is None
    # The publication and the configurator's own check: nobody else publishes.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(make_conninfo(w['f']['pg'], user='nexloop_api')) as api:
            api.execute('select control.nexloop_publish_world_glossary(%s,%s,%s,0)', (TENANT, V1.read_text(), digest))


def test_a_later_manifest_keeps_omitted_restores_null_and_clears_empty(glossary):
    g = glossary; admin, w = g['admin'], g['w']
    assert publish(w['configurator'], V1, 0)[0] == 0
    v2 = json.loads(V1.read_text())
    v2['manifest_version'] = 2
    v2['object_types'] = {'Consumer': '', 'Commitment': None}      # cleared by hand; restored to machine translation
    v2['properties'] = {}                                           # everything left out keeps its gloss
    path = g['tmp'] / 'v2.json'; path.write_text(json.dumps(v2, ensure_ascii=False))
    code, out = publish(w['configurator'], path, 1)
    assert code == 0 and out['result']['restored'] == 1, out
    assert row(admin, 'object_type', 'Consumer')[:2] == ('', 'manual')
    assert row(admin, 'object_type', 'Commitment') is None
    assert row(admin, 'property', 'external_id')[:2] == ('外部编号', 'manual')
    history = admin.execute("""select revision,cause,gloss,manifest_version from control.nexloop_world_glossary_revisions
        where tenant_id=%s and kind='object_type' and identifier='Commitment' order by revision""", (TENANT,)).fetchall()
    assert history == [(1, 'manifest', '承诺', 1), (2, 'restore', None, 2)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # the history is append-only
        with admin.transaction():
            admin.execute('set local role nexloop_owner')
            admin.execute("select set_config('eios.tenant_id',%s,true)", (TENANT,))
            admin.execute('delete from control.nexloop_world_glossary_revisions where tenant_id=%s', (TENANT,))


def test_the_owner_corrects_and_clears_a_gloss_and_nobody_else_can(glossary):
    g = glossary; admin, w = g['admin'], g['w']
    assert publish(w['configurator'], V1, 0)[0] == 0
    with w['client']() as client:
        csrf = workbench_login(client, w['operator']).json()['csrf_token']
        assert act(client, csrf, 'set_world_glossary', {'kind': 'object_type', 'identifier': 'Consumer', 'gloss': '顾客'}).status_code == 403
    with w['client']() as client:
        csrf = workbench_login(client, w['owner']).json()['csrf_token']
        done = act(client, csrf, 'set_world_glossary', {'kind': 'object_type', 'identifier': 'Consumer', 'gloss': '顾客'})
        assert done.status_code == 200, done.text
        cleared = act(client, csrf, 'set_world_glossary', {'kind': 'action', 'identifier': 'Consumer.create', 'gloss': ''})
        assert cleared.status_code == 200, cleared.text
        for bad in ({'kind': 'object_type', 'identifier': 'Consumer', 'gloss': '一' * 25},            # too long
                    {'kind': 'object_type', 'identifier': 'Consumer', 'gloss': 'Customer'},          # no Chinese
                    {'kind': 'object_type', 'identifier': 'Consumer', 'gloss': '客户', 'tenant_id': 'other'},  # exact keys
                    {'kind': 'object_type', 'identifier': 'Consumer'},
                    {'kind': 'tenant', 'identifier': 'Consumer', 'gloss': '客户'}):
            assert act(client, csrf, 'set_world_glossary', bad).status_code == 422, bad
        unknown = act(client, csrf, 'set_world_glossary', {'kind': 'object_type', 'identifier': 'NoSuchType', 'gloss': '无此类型'})
        assert unknown.status_code in (400, 409, 422) and row(admin, 'object_type', 'NoSuchType') is None
        model = glossed(client)
    consumer = next(t for t in model['object_types'] if t['name'] == 'Consumer')
    assert (consumer['gloss'], consumer['gloss_source']) == ('顾客', 'manual')
    create = next(a for a in model['actions'] if a['name'] == 'Consumer.create')
    assert row(admin, 'action', 'Consumer.create')[:2] == ('', 'manual') and (create['gloss'], create['gloss_source']) == ('', 'manual')
    correction = admin.execute("""select cause,actor_principal is not null,intent_id is not null from control.nexloop_world_glossary_revisions
        where tenant_id=%s and kind='object_type' and identifier='Consumer' order by revision desc limit 1""", (TENANT,)).fetchone()
    assert correction == ('correction', True, True)
    # The world is the entry's, never the body's: the handler refuses a simulation world even for the owner.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        owner_sql(admin, "select control.nexloop_governed_world_glossary_set(%s,'simulation',%s,'s','i',%s)",
                  (TENANT, w['owner']['principal_id'], Jsonb({'operation': 'set_world_glossary', 'kind': 'object_type', 'identifier': 'Consumer', 'gloss': '客户'})))


class StubModel:
    provider = 'deepseek'; model_id = 'deepseek-flash'

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.inputs, self.last_usage = answer, error, [], None

    @property
    def calls(self):
        return len(self.inputs)

    def complete(self, system_prompt, payload):
        assert system_prompt == SYSTEM_PROMPT
        self.inputs.append(payload)
        if self.error is not None:
            raise ExtractionProviderUnavailable('stand-in failure', self.error)
        self.last_usage = {'prompt_tokens': 120, 'completion_tokens': 8}
        item = json.loads(payload)
        return json.dumps({'gloss': self.answer(item) if callable(self.answer) else self.answer}, ensure_ascii=False)


@pytest.fixture
def translator(glossary):
    g = glossary; w = g['w']; f = w['f']
    with open_core(make_conninfo(f['pg'], user='nexloop_domain_worker')) as pool:
        _, token = seed_multi_authority(g['admin'], pool, [TRANSLATE], identity_suffix='-world-glossary')
        session = authenticate_service(pool, token, world='real')

        def make(model, **options):
            return WorldGlossaryTranslator(pool, session, f['reader'].signer, model, unavailable=ExtractionProviderUnavailable, **options)
        yield dict(g, make=make, pool=pool, session=session)


def test_missing_identifiers_are_machine_translated_from_metadata_only_and_priced(translator):
    t = translator; admin = t['admin']
    model = StubModel(answer=lambda item: '机翻' + str(abs(hash(item['identifier'])) % 1000))
    summary = t['make'](model, tariff=load_tariff(TARIFF, model='deepseek-flash')).run_once()
    assert summary['enqueued'] >= 1 and summary['translated'] == model.calls >= 1 and summary['quota'] == 'open'
    assert model.calls <= 10  # one round
    for payload in model.inputs:
        item = json.loads(payload)
        assert set(item) == {'identifier', 'kind', 'owner_type', 'description', 'display_name', 'terms', 'output'}
        assert all(item[k] is None or len(item[k]) <= CONTEXT_LIMIT for k in ('description', 'display_name'))
    # Nothing of any instance reaches the model: no stored object value and no Message text of the tenant.
    values = [v for (v,) in admin.execute("""select distinct value from ontology.objects o, jsonb_each_text(o.properties) p(key,value)
        where o.tenant_id=%s and char_length(value)>=4""", (TENANT,)).fetchall()]
    sent = ''.join(model.inputs)
    assert '请不要再给我发短信了' not in sent and not any(v in sent for v in values)  # the fixture customer's refusal message
    translated = admin.execute("""select g.kind,g.identifier,g.model_id,g.input_sha256 from control.nexloop_world_glossary g
        where g.tenant_id=%s and g.source='machine'""", (TENANT,)).fetchall()
    assert len(translated) == summary['translated'] and all(m == 'deepseek-flash' and len(d) == 64 for _, _, m, d in translated)
    usage = admin.execute("""select prompt_tokens,completion_tokens,amount,currency,outcome from control.nexloop_world_glossary_usage
        where tenant_id=%s""", (TENANT,)).fetchall()
    assert len(usage) == model.calls and all(u[:2] == (120, 8) and u[3] == 'CNY' and u[2] > 0 and u[4] == 'translated' for u in usage)
    costs = admin.execute("""select basis,cost_kind,currency from runtime.nexloop_cost_entries where tenant_id=%s and entry_id like 'model:world-glossary:%%'""",
                          (TENANT,)).fetchall()
    assert len(costs) == model.calls and set(costs) == {('configured_rate', 'model', 'CNY')}
    # The API shows them as machine glosses.
    w = t['w']
    with w['client']() as client:
        assert workbench_login(client, w['owner']).status_code == 200
        model_view = glossed(client)
    machine = [x for section in ('object_types', 'actions', 'functions', 'relation_types') for x in model_view[section] if x['gloss_source'] == 'machine']
    assert machine and all(x['gloss'].startswith('机翻') for x in machine)


def test_without_a_tariff_the_call_is_recorded_unpriced(translator):
    t = translator; admin = t['admin']
    model = StubModel(answer='机器译名')
    t['make'](model, per_round=1).run_once()
    assert model.calls == 1
    assert admin.execute("""select basis,amount from runtime.nexloop_cost_entries where tenant_id=%s and entry_id like 'model:world-glossary:%%'""",
                         (TENANT,)).fetchall() == [('unpriced', None)]
    assert admin.execute('select amount,prompt_tokens from control.nexloop_world_glossary_usage where tenant_id=%s', (TENANT,)).fetchall() == [(None, 120)]


def test_a_full_day_calls_the_model_zero_times(translator):
    t = translator; admin = t['admin']
    admin.execute("""insert into control.nexloop_world_glossary_quota(tenant_id,day,reserved) values(%s,(clock_timestamp() at time zone 'UTC')::date,50)""",
                  (TENANT,))
    model = StubModel(answer='机器译名')
    summary = t['make'](model).run_once()
    assert model.calls == 0 and summary['quota'] == 'full' and summary['translated'] == 0
    assert admin.execute('select count(*) from control.nexloop_world_glossary_usage where tenant_id=%s', (TENANT,)).fetchone()[0] == 0


def test_two_reservations_of_the_last_call_of_the_day_admit_one(translator):
    from concurrent.futures import ThreadPoolExecutor
    t = translator; admin = t['admin']; f = t['w']['f']
    port = GlossaryPort(t['pool'], t['session'], f['reader'].signer)
    port.call('scan')
    items = port.call('due', limit=2)['items']
    assert len(items) == 2
    admin.execute("""insert into control.nexloop_world_glossary_quota(tenant_id,day,reserved) values(%s,(clock_timestamp() at time zone 'UTC')::date,49)""",
                  (TENANT,))
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda item: port.call('reserve', kind=item['kind'], identifier=item['identifier']), items))
    assert sorted(a['allowed'] for a in answers) == [False, True] and [a.get('reason') for a in answers if not a['allowed']] == ['full']
    assert admin.execute('select reserved from control.nexloop_world_glossary_quota where tenant_id=%s', (TENANT,)).fetchone() == (50,)


def test_failures_back_off_then_fail_and_show_english_only(translator):
    t = translator; admin = t['admin']
    model = StubModel(error='http_429')
    t['make'](model, per_round=1).run_once()
    (kind, identifier, attempts, status, error), = admin.execute("""select kind,identifier,attempts,status,last_error from control.nexloop_world_glossary_jobs
        where tenant_id=%s and attempts>0""", (TENANT,)).fetchall()
    assert (attempts, status, error) == (1, 'pending', 'http_429') and row(admin, kind, identifier) is None
    for _ in range(2):  # the backoff passed
        admin.execute("update control.nexloop_world_glossary_jobs set next_attempt_at=clock_timestamp() where tenant_id=%s and kind=%s and identifier=%s",
                      (TENANT, kind, identifier))
        admin.execute("update control.nexloop_world_glossary_jobs set next_attempt_at=clock_timestamp()+interval '1 day' where tenant_id=%s and identifier<>%s",
                      (TENANT, identifier))
        t['make'](model, per_round=1).run_once()
    assert admin.execute('select attempts,status from control.nexloop_world_glossary_jobs where tenant_id=%s and kind=%s and identifier=%s',
                         (TENANT, kind, identifier)).fetchone() == (3, 'failed')
    assert row(admin, kind, identifier) is None
    # An answer that is not a gloss is a failure, never written.
    bad = StubModel(answer='Consumer')
    admin.execute("update control.nexloop_world_glossary_jobs set next_attempt_at=clock_timestamp() where tenant_id=%s and status='pending'", (TENANT,))
    summary = t['make'](bad, per_round=1).run_once()
    assert summary['failed'] == 1 and admin.execute("select count(*) from control.nexloop_world_glossary where tenant_id=%s and source='machine'",
                                                     (TENANT,)).fetchone()[0] == 0


def test_a_correction_between_the_call_and_its_record_stays_manual(translator):
    t = translator; admin, w = t['admin'], t['w']
    t['make'](StubModel(error='timeout'), per_round=1).run_once()  # one job exists and is pending
    kind, identifier = admin.execute("select kind,identifier from control.nexloop_world_glossary_jobs where tenant_id=%s and attempts=1", (TENANT,)).fetchone()
    admin.execute("update control.nexloop_world_glossary_jobs set next_attempt_at=clock_timestamp()+interval '1 day' where tenant_id=%s and identifier<>%s",
                  (TENANT, identifier))
    admin.execute("update control.nexloop_world_glossary_jobs set next_attempt_at=clock_timestamp() where tenant_id=%s and identifier=%s", (TENANT, identifier))

    class Racing(StubModel):
        def complete(self, system_prompt, payload):
            # The owner corrects the same identifier while the model is answering.
            with w['client']() as client:
                csrf = workbench_login(client, w['owner']).json()['csrf_token']
                assert act(client, csrf, 'set_world_glossary', {'kind': kind, 'identifier': identifier, 'gloss': '人工译名'}).status_code == 200
            return super().complete(system_prompt, payload)
    summary = t['make'](Racing(answer='机器译名'), per_round=1).run_once()
    assert summary['manual_kept'] == 1 and row(admin, kind, identifier)[:2] == ('人工译名', 'manual')


def test_the_translator_reaches_nothing_but_its_port(translator):
    t = translator; f = t['w']['f']
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker:
        for table in ('nexloop_world_glossary', 'nexloop_world_glossary_jobs', 'nexloop_world_glossary_usage', 'nexloop_world_glossary_quota'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), worker.transaction():
                worker.execute(f'select 1 from control.{table}')
        for function in ("control.nexloop_world_glossary_write_manual('t','object_type','x','客户','manifest',1,null,null)",
                         "control.nexloop_world_glossary_identifiers('t','real')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), worker.transaction():
                worker.execute('select ' + function)
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_api')) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute("select authz.nexloop_world_glossary_port('x','real','{}','x','{}')")
    # A forged or unsigned call is refused by the port itself, whatever the caller holds.
    with psycopg.connect(make_conninfo(f['pg'], user='nexloop_domain_worker')) as worker:
        claims = json.dumps({'protocol': 'nexloop-world-glossary-v1', 'operation': 'execute', 'tenant_id': TENANT,
                             'action_resource': 'eios:action:nexloop.claim.extract:1', 'parameters_digest': hashlib.sha256(b'{"verb":"scan"}').hexdigest()})
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("select authz.nexloop_world_glossary_port('x','real',%s,'x','{\"verb\":\"scan\"}')", (claims,))


def test_model_input_cuts_long_metadata():
    payload = json.loads(model_input({'kind': 'object_type', 'identifier': 'Consumer', 'owner_type': None, 'description': '长' * 500,
                                      'display_name': 'x' * 300}, {'Consumer': '客户'}))
    assert len(payload['description']) == CONTEXT_LIMIT and len(payload['display_name']) == CONTEXT_LIMIT


def test_the_service_runs_once_from_its_own_files_and_is_outside_the_application_budget():
    from nexloop_eios import background_services as B
    from nexloop_eios import connection_budget
    args = B._parser('world-glossary-translator').parse_args(['--database-url-file', 'd', '--signing-key-file', 's', '--service-credential-file', 'c',
        '--artifact-root', 'a', '--world', 'real', '--model-env-file', 'm', '--tariff-file', str(TARIFF), '--once'])
    assert args.once and args.pool_max == B.POOL_MAX and str(args.tariff_file) == str(TARIFF)
    assert 'world-glossary-translator' in B.ONE_SHOT and 'world-glossary-translator' not in connection_budget.required_services()
    assert load_tariff(None, model='deepseek-flash') is None and load_tariff(TARIFF, model='deepseek-flash')['currency'] == 'CNY'
