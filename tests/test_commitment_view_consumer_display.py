"""D3 (walkthrough 2026-10-18 batch 7): the optional consumer_display of the commitment-view contract."""
import pytest


def test_consumer_display_contract():
    """D3: commitment-view's optional consumer_display holds exactly the four name states (no name unless ok, a reason only when
    restricted)."""
    import json
    from pathlib import Path
    import jsonschema
    schema = json.loads((Path(__file__).resolve().parents[1] / 'packages/contracts/commitment-view.schema.json').read_text())['properties']['consumer_display']
    for good in ({'status': 'ok', 'name': '合成客户甲'}, {'status': 'restricted', 'reason': 'withheld'}, {'status': 'restricted', 'reason': 'forbidden'},
                 {'status': 'missing'}, {'status': 'failed'}):
        jsonschema.validate(good, schema)
    for bad in ({'status': 'ok'}, {'status': 'restricted'}, {'status': 'restricted', 'reason': 'withheld', 'name': 'x'}, {'status': 'missing', 'name': 'x'},
                {'status': 'failed', 'reason': 'forbidden'}, {'status': 'absent'}, {'status': 'ok', 'name': ''}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)
