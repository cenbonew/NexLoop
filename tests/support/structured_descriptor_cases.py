"""Claim → Consumer phase two (migration schema_structured_property, L6 condition C1): one table of structured descriptors that drives
both the SQL gate (runtime.nexloop_structured_descriptor_valid) and the F1 compatibility library
(nexloop_eios.evolution.schema_compat.structured_descriptor_valid), so the two judge every case the same way."""
import copy

from nexloop_eios.delivery_windows import DESCRIPTOR as VALID


def _with(edit):
    d = copy.deepcopy(VALID)
    edit(d)
    return d


def _field(**override):
    return {'name': 'slot', 'required': True, 'descriptor': {'kind': 'string', 'enum': ['a']}, **override}


ACCEPTED = [
    ('delivery_windows', VALID),
    ('one field one value, max 1', {'kind': 'array', 'max_items': 1, 'element': {'kind': 'struct', 'fields': [_field()]}}),
    ('eight fields, sixteen values, max 64', {'kind': 'array', 'max_items': 64, 'element': {'kind': 'struct', 'fields': [
        _field(name=f'f{i}', descriptor={'kind': 'string', 'enum': [f'v{j}' for j in range(16)]}) for i in range(8)]}}),
]

REFUSED = [
    # The four L6 asked for.
    ('extra key at the top', _with(lambda d: d.update(format=''))),
    ('extra key in the element', _with(lambda d: d['element'].update(enum=[]))),
    ('extra key in a field', _with(lambda d: d['element']['fields'][0].update(default='mon'))),
    ('extra key in a field descriptor', _with(lambda d: d['element']['fields'][0]['descriptor'].update(unit=''))),
    ('enum value too long', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append('x' * 33))),
    ('enum value not ascii', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append('周五'))),
    ('enum value with an accent', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append('café'))),
    ('empty enum', _with(lambda d: d['element']['fields'][0]['descriptor'].update(enum=[]))),
    ('missing max_items', _with(lambda d: d.pop('max_items'))),
    # The other hard limits.
    ('max_items above 64', _with(lambda d: d.update(max_items=65))),
    ('max_items zero', _with(lambda d: d.update(max_items=0))),
    ('max_items a boolean', _with(lambda d: d.update(max_items=True))),
    ('max_items a string', _with(lambda d: d.update(max_items='40'))),
    ('max_items not whole', _with(lambda d: d.update(max_items=40.5))),
    ('enum of seventeen', _with(lambda d: d['element']['fields'][0]['descriptor'].update(enum=[f'v{j}' for j in range(17)]))),
    ('enum value upper case', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append('Mon'))),
    ('enum value empty', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append(''))),
    ('enum value a number', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append(5))),
    ('duplicate enum value', _with(lambda d: d['element']['fields'][0]['descriptor']['enum'].append('mon'))),
    ('nine fields', {'kind': 'array', 'max_items': 8, 'element': {'kind': 'struct', 'fields': [_field(name=f'f{i}') for i in range(9)]}}),
    ('no fields', _with(lambda d: d['element'].update(fields=[]))),
    ('duplicate field name', _with(lambda d: d['element']['fields'].append(copy.deepcopy(d['element']['fields'][0])))),
    ('field name upper case', _with(lambda d: d['element']['fields'][0].update(name='Weekday'))),
    ('field name too long', _with(lambda d: d['element']['fields'][0].update(name='w' * 33))),
    ('field not required', _with(lambda d: d['element']['fields'][0].update(required=False))),
    ('field descriptor an integer enum', _with(lambda d: d['element']['fields'][0].update(descriptor={'kind': 'integer', 'enum': [1, 2]}))),
    ('field descriptor nested array', _with(lambda d: d['element']['fields'][0].update(
        descriptor={'kind': 'array', 'element': {'kind': 'string', 'enum': ['a']}}))),
    ('array of strings', {'kind': 'array', 'max_items': 4, 'element': {'kind': 'string', 'enum': ['a']}}),
    ('top level struct', {'kind': 'struct', 'max_items': 4, 'element': VALID['element']}),
    ('no descriptor', None),
    ('descriptor not an object', ['array']),
]
