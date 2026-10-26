"""tests/support/absence.py: a seeded value is found wherever it is; a random digest or id that happens to contain it is not."""
import json

from support.absence import absent


def test_absent_sees_values_and_ignores_hex_like_ids():
    digest = '00a9000ff' + 'e' * 55          # a sha256-shaped value that contains "9000"
    assert '9000' in json.dumps({'object_type_digest': digest})
    assert absent(9000, json.dumps({'object_type_digest': digest, 'id': '9000ab12-0000-4000-8000-000000009000'}))
    for present in ({'income': 9000}, {'income': '9000'}, {'note': 'income 9000 yuan'}, [{'v': 9000.0}], {'9000': 1}):
        assert not absent(9000, present), present
    assert not absent('13800000000', {'contact': 'tel:13800000000'}) and absent('13800000000', {'hmac': 'a13800000000' + 'f' * 52})
    assert absent(5000, {'level': 5, 'flag': True, 'none': None})
