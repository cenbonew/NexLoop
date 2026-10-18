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
    def __init__(self, configured=True, limit='0.000010', used='0'):
        self.limit, self.used, self.configured, self.reserved = Decimal(limit), Decimal(used), configured, []

    def budget_status(self, kind):
        assert kind == 'model'
        if not self.configured:
            return {'configured': False, 'exhausted': True}
        return {'configured': True, 'limit_amount': str(self.limit), 'used': str(self.used), 'exhausted': self.used >= self.limit}

    def reserve_budget(self, **kw):
        self.reserved.append(kw)
        self.used += Decimal(kw['amount'])
        return {'consumption_id': kw['consumption_id']}


class Provider:
    profile_id = MODEL + '@1024'
    model = MODEL

    def __init__(self, tokens):
        self.usage_tokens = tokens


def test_the_budget_records_the_tokens_caps_at_the_limit_and_then_refuses(tmp_path):
    tariff = load_tariff(tariff_file(tmp_path, per_million_tokens='1'))
    controls = Controls()
    budget = EmbeddingBudget(None, None, tariff=tariff, service='recall-indexer', controls=controls)
    assert budget.allowed() is True
    assert budget.record(Provider(4))['consumption_id'].startswith('embedding:recall-indexer:')
    assert controls.reserved[0]['amount'] == '0.000004' and controls.reserved[0]['unit'] == 'CNY' and controls.reserved[0]['budget_kind'] == 'model'
    assert controls.reserved[0]['source_ref'] == f'embedding:{MODEL}@1024:4'
    provider = Provider(10)
    budget.record(provider)
    assert controls.reserved[1]['amount'] == '0.000006' and provider.usage_tokens == 0  # the rest of the limit, then reset
    assert budget.allowed() is False  # exhausted: the next tick makes no embedding call
    assert budget.record(Provider(0)) is None
    # No model budget configured: nothing reserved and never refused (as for Runs).
    free = EmbeddingBudget(None, None, tariff=tariff, service='claim-matcher', controls=Controls(configured=False))
    assert free.allowed() is True and free.record(Provider(100)) is None
    with pytest.raises(ValueError):
        EmbeddingBudget(None, None, tariff=tariff, service='other', controls=controls)


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
