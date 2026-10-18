"""Owner 2026-10-18 (option B: vector recall and candidate merge on stage). Unit part: the embedding tariff, the cost and the
budget record, the Ark provider's token count, the services' startup checks and the doctor's file checks. No provider call."""
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from nexloop_eios import doctor
from nexloop_eios.background_services import _parser, embedding_budget, load_embedding
from nexloop_eios.embedding_budget import EmbeddingBudget, EmbeddingTariffError, load_tariff
from nexloop_eios.embedding_profile import load_embedding_profile
from nexloop_eios.embedding_provider import ArkMultimodalEmbeddingProvider

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'doubao-embedding-vision-251215'
LOCATION = 'https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal'
SYNTHETIC_KEY = 'synthetic-embedding-key-never-real'


def private(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    os.chmod(path, 0o600)
    return path


def env_file(tmp_path, *, dimension='1024', key=SYNTHETIC_KEY, model=MODEL):
    return private(tmp_path, 'embedding.env', f'EMBEDDING_MODEL={model}\nEMBEDDING_MODE=multimodal\nEMBEDDING_LOCATION={LOCATION}\n'
                   f'EMBEDDING_DIMENSION={dimension}\nEMBEDDING_API_KEY={key}\n')


def tariff_file(tmp_path, **override):
    value = {'schema': 'nexloop-embedding-tariff/1', 'model': MODEL, 'currency': 'CNY', 'per_million_tokens': '0.70', **override}
    return private(tmp_path, 'tariff.json', json.dumps(value))


def test_the_tariff_is_checked_and_costs_round_up(tmp_path):
    tariff = load_tariff(tariff_file(tmp_path), model=MODEL)
    assert tariff.cost(1) == Decimal('0.000001') and tariff.cost(1_000_000) == Decimal('0.700000') and tariff.cost(1500) == Decimal('0.001050')
    for bad in ({'per_million_tokens': 0.7}, {'per_million_tokens': '-1'}, {'per_million_tokens': 'NaN'}, {'currency': 'cny'},
                {'schema': 'x'}, {'model': 'other'}, {'extra': 1}):
        with pytest.raises(EmbeddingTariffError):
            load_tariff(tariff_file(tmp_path, **bad), model=MODEL)
    loose = tmp_path / 'loose.json'
    loose.write_text(json.dumps({'schema': 'nexloop-embedding-tariff/1', 'model': MODEL, 'currency': 'CNY', 'per_million_tokens': '1'}))
    os.chmod(loose, 0o644)
    with pytest.raises(EmbeddingTariffError):  # private file only
        load_tariff(loose)


class Controls:
    def __init__(self, configured=True, limit='0.000010', used='0', in_period=True):
        self.limit, self.used, self.configured, self.in_period, self.reserved = Decimal(limit), Decimal(used), configured, in_period, []

    def budget_open(self, kind):
        assert kind == 'model'
        if not self.configured:
            return {'configured': False, 'open': False}
        return {'configured': True, 'open': self.in_period and self.used < self.limit, 'used': str(self.used), 'limit_amount': str(self.limit)}

    def reserve_budget(self, **kw):
        # As authz.nexloop_reserve_budget: the period and the limit are checked atomically.
        if not self.in_period or self.used + Decimal(kw['amount']) > self.limit:
            raise RuntimeError('NXB01')
        self.reserved.append(kw)
        self.used += Decimal(kw['amount'])
        return {'consumption_id': kw['consumption_id']}


class Provider:
    profile_id = MODEL + '@1024'
    model = MODEL
    dimension = 1024

    def __init__(self, tokens_per_call=None):
        self.usage_tokens, self.calls, self.per_call = 0, 0, tokens_per_call

    def embed(self, text):
        self.calls += 1
        self.usage_tokens += self.per_call if self.per_call is not None else len(text)
        return (1.0,)


def test_each_call_is_reserved_before_the_provider_and_refused_without_calling_it(tmp_path):
    from nexloop_eios.embedding_budget import EmbeddingBudgetDenied
    tariff = load_tariff(tariff_file(tmp_path, per_million_tokens='1'))
    controls = Controls()
    budget = EmbeddingBudget(None, None, tariff=tariff, service='recall-indexer', controls=controls)
    assert budget.allowed() is True
    provider = Provider()
    wrapped = budget.wrap(provider)
    assert wrapped.profile_id == MODEL + '@1024' and wrapped.dimension == 1024
    wrapped.embed('四个汉字')  # 4 characters: 0.000004 reserved before the call
    assert controls.reserved[0]['amount'] == '0.000004' and controls.reserved[0]['budget_kind'] == 'model' and controls.reserved[0]['unit'] == 'CNY'
    assert controls.reserved[0]['consumption_id'].startswith('embedding:recall-indexer:') and provider.calls == 1
    with pytest.raises(EmbeddingBudgetDenied):
        wrapped.embed('七个字的一段文本')  # 8 > the 6 left: refused before any provider call
    assert provider.calls == 1 and len(controls.reserved) == 1
    # More tokens than estimated: the difference is reserved after the call (capped at what is left).
    more = Provider(tokens_per_call=5)
    wrapped = EmbeddingBudget(None, None, tariff=tariff, service='claim-matcher', controls=Controls(limit='0.000010')).wrap(more)
    wrapped.embed('ab')
    assert [r['amount'] for r in wrapped._budget.controls.reserved] == ['0.000002', '0.000003']


def test_a_budget_outside_its_period_or_used_up_claims_nothing_and_no_budget_is_free(tmp_path):
    tariff = load_tariff(tariff_file(tmp_path, per_million_tokens='1'))
    assert EmbeddingBudget(None, None, tariff=tariff, service='recall-indexer', controls=Controls(in_period=False)).allowed() is False
    assert EmbeddingBudget(None, None, tariff=tariff, service='recall-indexer', controls=Controls(used='0.000010')).allowed() is False
    free = EmbeddingBudget(None, None, tariff=tariff, service='claim-matcher', controls=Controls(configured=False))
    provider = Provider()
    assert free.allowed() is True and free.wrap(provider).embed('x') == (1.0,) and free.controls.reserved == []
    with pytest.raises(ValueError):
        EmbeddingBudget(None, None, tariff=tariff, service='other', controls=Controls())


def test_the_ark_provider_counts_reported_tokens_or_bounds_them_by_characters(tmp_path):
    profile = load_embedding_profile({}, env_file=env_file(tmp_path, dimension='4'))
    answers = [{'data': {'embedding': [0.1, 0.2, 0.3, 0.4]}, 'usage': {'prompt_tokens': 7, 'total_tokens': 7}},
               {'data': {'embedding': [0.1, 0.2, 0.3, 0.4]}}]
    provider = ArkMultimodalEmbeddingProvider(profile, transport=lambda url, headers, body, timeout: (200, json.dumps(answers.pop(0)).encode()))
    provider.embed('防水登山鞋');provider.embed('SKU-1001')
    assert provider.calls == 2 and provider.usage_tokens == 7 + len('SKU-1001')


def test_services_need_a_tariff_only_when_embedding_is_enabled(tmp_path):
    empty = private(tmp_path, 'empty.env', '')
    assert load_embedding(empty) == (None, None)  # the stage placeholder: FTS + n-gram mode
    args = _parser('recall-indexer').parse_args(['--database-url-file', 'x', '--service-credential-file', 'x', '--signing-key-file', 'x',
                                                 '--signing-key-id', 'k', '--artifact-root', 'x', '--world', 'real', '--types-file', 'x',
                                                 '--embedding-env-file', str(empty)])
    assert embedding_budget(None, None, args, None, 'recall-indexer') is None
    args.embedding_env_file = env_file(tmp_path)
    embedding, dimension = load_embedding(args.embedding_env_file)
    assert dimension == 1024 and embedding.profile_id == MODEL + '@1024'
    with pytest.raises(ValueError):
        embedding_budget(None, None, args, embedding, 'recall-indexer')  # no tariff
    args.embedding_tariff_file = tariff_file(tmp_path, model='other-model')
    with pytest.raises(EmbeddingTariffError):
        embedding_budget(None, None, args, embedding, 'recall-indexer')  # the tariff is for another model
    assert _parser('claim-matcher').parse_args(['--database-url-file', 'x', '--service-credential-file', 'x', '--signing-key-file', 'x',
                                                '--signing-key-id', 'k', '--artifact-root', 'x', '--world', 'real', '--model-env-file', 'x',
                                                '--match-config-file', 'x', '--embedding-env-file', 'e', '--embedding-tariff-file', 't']).embedding_tariff_file == Path('t')


def test_the_doctor_checks_the_files_against_the_merge_configuration_and_never_prints_the_key(tmp_path):
    merge = ROOT / 'deploy/ontology/merge-config.v1.json'
    result = doctor.embedding(env_file(tmp_path), tariff_file(tmp_path), merge)
    assert all(result['checks'].values()) and result['details']['embedding_profile'] == MODEL + '@1024'
    assert SYNTHETIC_KEY not in json.dumps(result)
    wrong = doctor.embedding(env_file(tmp_path, dimension='2048'), tariff_file(tmp_path), merge)
    assert wrong['checks']['embedding_profile_matches_merge_configuration'] is False
    disabled = doctor.embedding(private(tmp_path, 'blank.env', ''), tariff_file(tmp_path), merge)
    assert disabled['checks']['embedding_enabled'] is False
    no_key = doctor.embedding(env_file(tmp_path, key=''), tariff_file(tmp_path), merge)
    assert no_key['checks']['embedding_enabled'] is False


def test_the_install_script_reads_the_key_from_a_pipe_only_and_asks_for_the_price():
    text = (ROOT / 'scripts/deploy/install_embedding_key.sh').read_text()
    assert 'EMBEDDING_PRICE_PER_MILLION:?' in text and "[ ! -t 0 ]" in text and 'shred -u' in text
    assert 'echo "$KEY' not in text and 'set +x' in text
    assert MODEL in text and '1024' in text and LOCATION in text


def test_text_tokens_are_counted_and_image_tokens_for_a_text_input_are_refused(tmp_path):
    """Ark bills text and image input tokens separately; only text is sent: image tokens must be 0."""
    from nexloop_eios.embedding_provider import EmbeddingUnavailable
    profile = load_embedding_profile({}, env_file=env_file(tmp_path, dimension='4'))
    vector = [0.1, 0.2, 0.3, 0.4]
    answers = [{'data': {'embedding': vector}, 'usage': {'prompt_tokens': 9, 'prompt_tokens_details': {'text_tokens': 6, 'image_tokens': 0}, 'total_tokens': 9}},
               {'data': {'embedding': vector}, 'usage': {'prompt_tokens': 30, 'prompt_tokens_details': {'text_tokens': 5, 'image_tokens': 25}, 'total_tokens': 30}}]
    provider = ArkMultimodalEmbeddingProvider(profile, transport=lambda url, headers, body, timeout: (200, json.dumps(answers.pop(0)).encode()))
    assert provider.embed('防水登山鞋') and provider.usage_tokens == 6
    with pytest.raises(EmbeddingUnavailable, match='image tokens'):
        provider.embed('SKU-1001')
    assert provider.usage_tokens == 6 + 5 + 25  # counted, never hidden
