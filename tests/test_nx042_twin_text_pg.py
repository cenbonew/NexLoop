"""NX-042 T6 (design §2, D1, D5, D6): the text layer of the consumer twin.

The twin's step text comes from its version's generator: the deterministic template twin-template/1 by default - the same seed and
twin version give the same text byte for byte (here across two worlds with the same synthetic Consumers); every text carries the
simulation marker and no real-looking content; the event's attributes carry the marker, the template and the generator. The model
path is an interface only and is refused (model_budget_required), in SQL and in Python. Real and test worlds are refused before any
lookup. Real PostgreSQL; fixtures from the T3 tests.
"""
import uuid

import psycopg
import pytest

from nexloop_eios import twin_port as T
from test_nx041_s7_pg import consumers  # noqa: F401
from test_nx042_twin_port_pg import TWIN, port, real_rows, t3, s7  # noqa: F401

MARKER = '【模拟消息】'


def texts(acts):
    return [(a['consumer_id'], a['step'], a['event_kind'], (a['event'] or {}).get('content', {}).get('text')) for a in acts]


def test_the_template_text_is_reproducible_byte_for_byte_and_marked_simulated(t3):
    f = t3
    admin = f['admin']
    before = real_rows(admin)
    twin = T.ConsumerTwin(TWIN, 1, 'text-1')
    people = consumers(admin, f['a'])
    a = port(f).run(twin, people, steps=6)
    b = port(f, world=f['b'], token=f['token_b']).run(twin, consumers(admin, f['b']), steps=6)
    assert texts(a) == texts(b)  # another world, the same seed and version: the same texts
    spoken = [x for x in a if x['event_kind'] in ('message', 'refuse_contact', 'payment_intent')]
    assert spoken and {x['event_kind'] for x in spoken} >= {'message'}
    templates = admin.execute('select runtime.nexloop_twin_text_templates()').fetchone()[0]
    known = {t['id']: t['text'] for kind in templates.values() for t in kind}
    for x in spoken:
        content = x['event']['content']
        attrs = content['attributes']
        assert content['text'].startswith(MARKER) and known[attrs['text_template']] == content['text']
        assert (attrs['text_marker'], attrs['text_generator']) == ('simulated', T.TEMPLATE_GENERATOR)
        assert attrs['text_template'].split('.')[0] == x['event_kind']
    for x in a:
        if x['event_kind'] == 'visit':
            assert 'text' not in x['event']['content'] and 'text_template' not in x['event']['content']['attributes']
    # Another seed gives other texts somewhere (the draw is seeded), still from the templates.
    c = port(f).run(T.ConsumerTwin(TWIN, 1, 'text-2'), people, steps=6)
    assert texts(c) != texts(a)
    assert real_rows(admin) == before  # nothing in real


def test_every_template_is_marked_and_holds_no_real_looking_content(t3):
    admin = t3['admin']
    templates = admin.execute('select runtime.nexloop_twin_text_templates()').fetchone()[0]
    assert set(templates) == {'message', 'refuse_contact', 'payment_intent'}
    for kind, items in templates.items():
        assert items and len({t['id'] for t in items}) == len(items)
        for t in items:
            assert t['id'].startswith(kind + '.') and t['text'].startswith(MARKER) and 1 <= len(t['text']) <= 200
            # D5 guard (T1): no email, phone, member number or identifier digest.
            assert admin.execute('select runtime.nexloop_twin_text_looks_real(%s)', (t['text'],)).fetchone()[0] is False
            assert not any(word in t['text'] for word in ('先生', '女士', '@', 'http', '电话', '微信'))


def render(admin, generator, kind='message', step=1, behavior='reply_within_24h'):
    return admin.execute("select runtime.nexloop_twin_render_text(%s,'base',%s,%s,%s)", (generator, step, behavior, kind)).fetchone()[0]


def test_the_model_path_is_an_interface_only_and_refused(t3):
    admin = t3['admin']
    assert render(admin, 'twin-template/1')['template_id'].startswith('message.')
    assert render(admin, 'twin-template/1') == render(admin, 'twin-template/1')
    assert render(admin, 'twin-template/1', kind='visit')['text'] is None
    # A model generator listed by trusted configuration: rendering with it is refused before anything else.
    admin.execute("insert into control.nexloop_twin_generators(generator_version,uses_model,budget_account) values('twin-model/1',true,'simulation')")
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin text: model_budget_required'), admin.transaction():
        render(admin, 'twin-model/1')
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='twin text: generator_unknown'), admin.transaction():
        render(admin, 'twin-unlisted/1')
    # Python: the model generator is an interface whose render always refuses.
    with pytest.raises(T.ModelBudgetRequired, match='model_budget_required'):
        T.ModelTextGenerator('twin-model/1').render(behavior='reply_within_24h', kind='message', seed='s', step=1)
    # No application role executes the renderer directly (only act, as its owner).
    for role in ('nexloop_twin_port', 'nexloop_sim_api', 'nexloop_api', 'nexloop_domain_worker'):
        assert not admin.execute("select has_function_privilege(%s,'runtime.nexloop_twin_render_text(text,text,integer,text,text)','execute')", (role,)).fetchone()[0]


def test_real_and_test_worlds_are_refused(t3):
    f = t3
    for world in ('real', 'test'):
        with pytest.raises(ValueError):
            port(f, world=world)  # the Python port refuses before any call
        with f['role']('nexloop_twin_port')() as db, pytest.raises(psycopg.Error):
            db.execute('select authz.nexloop_twin_act(%s,%s,%s,%s,1)', (__import__('hashlib').sha256(f['token_a'].encode()).hexdigest(), world,
                                                                         'a' * 64, 'b' * 64))
