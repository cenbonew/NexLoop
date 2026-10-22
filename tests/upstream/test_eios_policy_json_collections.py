"""JSON arrays preserve strict AST collection and scalar contracts."""

import json

import pytest

from eios.authz.policy import parse_policy_expression, parse_policy_plan
from eios.authz.policy.validator import PolicyValidationError
from eios.authz.policy.canonical import canonicalize_policy_expression


def _eq():
    return {
        "op": "eq",
        "node_id": "equal",
        "left": {"kind": "application", "attribute": "actor_kind"},
        "right": {"kind": "literal", "value": "agent"},
    }


@pytest.mark.parametrize("operator", ["all", "any"])
def test_json_collection_roundtrips_through_expression_and_plan(operator):
    document = {"op": operator, "node_id": "collection", "children": [_eq()]}
    expression = parse_policy_expression(json.dumps(document))
    assert type(expression.children) is tuple
    canonical = canonicalize_policy_expression(expression)
    assert (
        canonicalize_policy_expression(parse_policy_expression(canonical.utf8))
        == canonical
    )
    plan = parse_policy_plan(
        json.dumps({"policy_id": "echo", "revision": 1, "condition": document})
    )
    assert plan.condition == expression


def test_literal_json_collection_preserves_scalar_types():
    document = _eq()
    document["right"]["value"] = [True, 1, "1"]
    expression = parse_policy_expression(json.dumps(document))
    assert expression.right.value == (True, 1, "1")
    assert tuple(type(item) for item in expression.right.value) == (bool, int, str)


@pytest.mark.parametrize(
    "malformation",
    [
        "empty",
        "wrong_children",
        "nested_literal",
        "unknown",
        "wrong_attribute",
        "duplicate_nodes",
    ],
)
def test_json_conversion_does_not_relax_invalid_policy(malformation):
    document = {"op": "all", "node_id": "collection", "children": [_eq()]}
    if malformation == "empty":
        document["children"] = []
    elif malformation == "wrong_children":
        document["children"] = "equal"
    elif malformation == "nested_literal":
        document["children"][0]["right"]["value"] = [["agent"]]
    elif malformation == "unknown":
        document["children"][0]["unknown"] = []
    elif malformation == "wrong_attribute":
        document["children"][0]["left"]["attribute"] = ["actor_kind"]
    else:
        document["children"].append(_eq())
    with pytest.raises(PolicyValidationError):
        parse_policy_expression(json.dumps(document))


def test_json_plan_revision_stays_strict():
    with pytest.raises(PolicyValidationError):
        parse_policy_plan(
            json.dumps({"policy_id": "echo", "revision": "1", "condition": _eq()})
        )
