"""D23 option B, batch 0 (docs/implementation/d23-option-b-design.md §1, C1): candidate Action input schemas derived from the workbench
operation specs (workbench_actions_http.OPERATIONS), never wider in what they refuse than the runtime checks.

Each operation's spec is an exact field set (a body with another set is refused) and one check per field. A check this module knows
becomes its JSON Schema; one it does not know becomes {} - the field is required but unconstrained, so the schema can only be wider
than the runtime check, never stricter:

    _id(re.compile(p))   {"type": "string", "pattern": "^(p)$"} when p uses only the dialect both Python and ECMA-262 read alike
    _bool                {"type": "boolean"}
    _text                {"type": "string", "minLength": 1, "maxLength": 255}
    _reason              {"type": "string", "minLength": 1, "maxLength": 500}
    _scope               {"enum": [...]}
    _kind(name)          {"const": name}
    _utc, _utc_text      {"type": "string"}   (no "format": dispatch validates with the format checker on, schema_gates does not)

Anchors (C1): a pattern is exported as ^(p)$. The runtime check uses re.fullmatch; jsonschema applies "pattern" with re.search, where
Python's $ also matches before one final newline - so "abc\\n" passes the schema and fails the runtime check. Deliberately wider.
A pattern outside the safe dialect (lookaround, inline flags, backreferences, \\b, \\A, \\Z, any escaped letter) is refused, left for
a person, never guessed. A spec chosen by the body (a callable spec) is refused too.

Only the HTTP fields are derived here; the request body the governed Action validates is built by GoalGovernedActions.<operation>
(request_id, operation and the fields, sometimes transformed). **operation_schema's result is never published as it is** (L6 C4):
from batch 1 on, each Action's schema is derived from (or compared item by item with) the body GoalGovernedActions builds, and its
test asserts that the bodies those methods build pass the candidate (design §3, §5).
A runtime pattern compiled with flags other than the default (re.UNICODE) is refused (C3).
"""
import re

from nexloop_eios import workbench_actions_http as W

_SAFE = re.compile(r'(?:[^\\(]|\\[.\-+*?^$|()\[\]{}/]|\((?!\?))*')


class NotDerivable(ValueError):
    """The spec or a pattern needs a person: never guessed."""


def safe_pattern(pattern):
    """The Python pattern exported as ^(p)$, or NotDerivable when its dialect is not read alike by Python and ECMA-262."""
    if not isinstance(pattern, str) or not _SAFE.fullmatch(pattern) or '(?' in pattern:
        raise NotDerivable('pattern dialect')
    return '^(' + pattern + ')$'


def _cell(check, kind):
    for cell in check.__closure__ or ():
        if isinstance(cell.cell_contents, kind):
            return cell.cell_contents
    return None


def field_schema(check):
    """The JSON Schema of one field check; {} for a check this module does not know (wider, never stricter)."""
    if check is W._bool:
        return {'type': 'boolean'}
    if check is W._text:
        return {'type': 'string', 'minLength': 1, 'maxLength': 255}
    if check is W._reason:
        return {'type': 'string', 'minLength': 1, 'maxLength': 500}
    if check is W._scope:
        return {'enum': ['tenant', 'role', 'consumer', 'strategy', 'action_type']}
    if check in (W._utc, W._utc_text):
        return {'type': 'string'}
    code = getattr(check, '__code__', None)
    if code is not None and code is W._id(re.compile('x')).__code__:
        compiled = _cell(check, re.Pattern)
        if compiled.flags != re.UNICODE:  # C3: IGNORECASE, VERBOSE, DOTALL... change what the text means; JSON Schema has no flags
            raise NotDerivable('pattern flags')
        return {'type': 'string', 'pattern': safe_pattern(compiled.pattern)}
    if code is not None and code is W._kind('x').__code__:
        return {'const': _cell(check, str)}
    return {}


def operation_schema(operation):
    """The candidate schema of one workbench operation's HTTP fields: exact field set, each field's derived schema. A starting point
    for a batch, never a publishable Action input schema: that one validates the body GoalGovernedActions builds (C4)."""
    spec = W.OPERATIONS[operation]
    if callable(spec):
        raise NotDerivable('spec chosen by the body')
    return {'type': 'object', 'additionalProperties': False, 'required': sorted(spec),
            'properties': {name: field_schema(check) for name, check in sorted(spec.items())}}


def derive_all():
    """{operation: schema} and {operation: reason} for every workbench operation."""
    schemas, refused = {}, {}
    for operation in sorted(W.OPERATIONS):
        try:
            schemas[operation] = operation_schema(operation)
        except NotDerivable as error:
            refused[operation] = str(error)
    return schemas, refused
