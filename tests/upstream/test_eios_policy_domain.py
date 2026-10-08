from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3

from hypothesis import given, strategies as st
import pytest
from pydantic import ValidationError

from eios.authz.policy import (
    ActionParameterRef,
    All,
    AnyOf,
    ApplicationRef,
    Contains,
    Eq,
    EvaluationContext,
    Exists,
    Gt,
    Gte,
    In,
    LiteralValue,
    Lt,
    Lte,
    Neq,
    Not,
    ObjectPropertyDefinition,
    ObjectPropertyRef,
    ObjectPropertyType,
    ObjectSchema,
    PolicyEvaluationResult,
    PolicyPlan,
    PolicySqlCompileError,
    PolicyValidationError,
    ResourceRef,
    SqlDialect,
    SubjectRef,
    Subset,
    TrustedTimeRef,
    compile_policy_sql,
    evaluate_policy,
    parse_policy_expression,
    parse_policy_plan,
    validate_policy_expression,
)


NOW = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


def _literal(value: object) -> LiteralValue:
    return LiteralValue(kind="literal", value=value)


def _subject(attribute: str = "department") -> SubjectRef:
    return SubjectRef(kind="subject", attribute=attribute)


def _property(
    property_name: str = "classification",
    object_type: str = "Document",
) -> ObjectPropertyRef:
    return ObjectPropertyRef(
        kind="object_property",
        object_type=object_type,
        property_name=property_name,
    )


def _context(**changes: object) -> EvaluationContext:
    values: dict[str, object] = {
        "subject": {"department": "finance", "clearance": 4},
        "application": {"environment": "production"},
        "resource": {"classification": "internal"},
        "object_properties": {
            ("Document", "classification"): "internal",
            ("Document", "rank"): 4,
            ("Document", "tags"): ("finance", "approved"),
        },
        "action_parameters": {"requested_export": False},
        "trusted_now": NOW,
    }
    values.update(changes)
    return EvaluationContext(**values)


def _schema() -> ObjectSchema:
    return ObjectSchema(
        definitions=(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="classification",
                value_type=ObjectPropertyType.STRING,
            ),
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="rank",
                value_type=ObjectPropertyType.INTEGER,
            ),
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="tags",
                value_type=ObjectPropertyType.STRING_SET,
            ),
        )
    )


def _sqlite_row_matches(
    expression: object,
    schema: ObjectSchema,
    properties: dict[str, object] | str,
) -> bool:
    compiled = compile_policy_sql(expression, schema, dialect=SqlDialect.SQLITE)
    encoded = properties if type(properties) is str else json.dumps(properties)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?)",
            ("Document", encoded),
        )
        observed = connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM objects WHERE {compiled.predicate})",
            compiled.parameters,
        ).fetchone()[0]
    finally:
        connection.close()
    return bool(observed)


def test_reference_vocabulary_is_closed_and_strict() -> None:
    assert SubjectRef(kind="subject", attribute="department").kind == "subject"
    assert ApplicationRef(kind="application", attribute="environment").kind == (
        "application"
    )
    assert ResourceRef(kind="resource", attribute="classification").kind == "resource"
    assert _property().kind == "object_property"
    assert (
        ActionParameterRef(
            kind="action_parameter", parameter_name="requested_export"
        ).kind
        == "action_parameter"
    )
    assert TrustedTimeRef(kind="trusted_time", value="now").kind == "trusted_time"

    with pytest.raises(ValidationError, match="literal_error"):
        SubjectRef(kind="resource", attribute="department")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SubjectRef(kind="subject", attribute="department", path="escape")
    with pytest.raises(ValidationError, match="string_type"):
        SubjectRef(kind="subject", attribute=7)


def test_ast_is_frozen_discriminated_and_rejects_unknown_operators() -> None:
    expression = Eq(
        op="eq",
        node_id="department-match",
        left=_subject(),
        right=_literal("finance"),
    )
    with pytest.raises(ValidationError, match="frozen"):
        expression.node_id = "changed"

    with pytest.raises(PolicyValidationError, match="policy document is invalid"):
        parse_policy_expression(
            json.dumps(
                {
                    "op": "exec",
                    "node_id": "escape",
                    "left": {"kind": "subject", "attribute": "department"},
                    "right": {"kind": "literal", "value": "finance"},
                }
            )
        )


def test_parser_rejects_duplicates_extra_fields_and_non_json_inputs() -> None:
    valid = (
        '{"op":"exists","node_id":"x","value":'
        '{"kind":"subject","attribute":"department"}}'
    )
    assert isinstance(parse_policy_expression(valid), Exists)

    duplicate = valid.replace('"node_id":"x"', '"node_id":"x","node_id":"y"')
    for raw in (
        duplicate,
        valid[:-1] + ',"unknown":true}',
        '{"op":"eq","node_id":"x","left":NaN,"right":null}',
    ):
        with pytest.raises(PolicyValidationError, match="policy document is invalid"):
            parse_policy_expression(raw)

    with pytest.raises(PolicyValidationError, match="policy document is invalid"):
        parse_policy_expression({"op": "exists"})  # type: ignore[arg-type]


def test_policy_plan_parser_is_strict_and_returns_defensive_copy() -> None:
    raw = json.dumps(
        {
            "policy_id": "policy-document-read",
            "revision": 3,
            "condition": {
                "op": "eq",
                "node_id": "department-match",
                "left": {"kind": "subject", "attribute": "department"},
                "right": {"kind": "literal", "value": "finance"},
            },
        }
    )
    plan = parse_policy_plan(raw)
    assert plan == PolicyPlan(
        policy_id="policy-document-read",
        revision=3,
        condition=Eq(
            op="eq",
            node_id="department-match",
            left=_subject(),
            right=_literal("finance"),
        ),
    )

    decoded = json.loads(raw)
    decoded["condition"]["node_id"] = "changed"
    assert plan.condition.node_id == "department-match"


def test_parser_preserves_explicit_datetime_literal_for_trusted_time() -> None:
    expression = parse_policy_expression(
        json.dumps(
            {
                "op": "lt",
                "node_id": "before-deadline",
                "left": {"kind": "trusted_time", "value": "now"},
                "right": {
                    "kind": "literal",
                    "value_type": "datetime",
                    "value": "2026-07-22T16:00:00+08:00",
                },
            }
        )
    )

    assert type(expression.right.value) is datetime
    assert expression.right.value == datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    assert evaluate_policy(expression, _context()).matched is True


def test_datetime_shaped_string_is_not_guessed_without_explicit_type() -> None:
    expression = parse_policy_expression(
        json.dumps(
            {
                "op": "eq",
                "node_id": "opaque-string",
                "left": {"kind": "subject", "attribute": "opaque"},
                "right": {
                    "kind": "literal",
                    "value": "2026-07-22T08:00:00Z",
                },
            }
        )
    )

    assert type(expression.right.value) is str


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-07-22T08:00:00.1234567Z",
        "2026-07-22T08:00:00+25:00",
        "2026-07-22 08:00:00Z",
    ),
)
def test_explicit_datetime_rejects_lossy_or_invalid_rfc3339(
    timestamp: str,
) -> None:
    raw = json.dumps(
        {
            "op": "lt",
            "node_id": "invalid-time",
            "left": {"kind": "trusted_time", "value": "now"},
            "right": {
                "kind": "literal",
                "value_type": "datetime",
                "value": timestamp,
            },
        }
    )
    with pytest.raises(PolicyValidationError, match="policy document is invalid"):
        parse_policy_expression(raw)


def test_datetime_collections_are_explicitly_unsupported() -> None:
    with pytest.raises(ValidationError):
        LiteralValue(kind="literal", value=NOW)
    with pytest.raises(ValidationError):
        LiteralValue(
            kind="literal",
            value_type="datetime",
            value=(NOW,),
        )
    with pytest.raises(ValidationError):
        LiteralValue(kind="literal", value=(NOW,))


@pytest.mark.parametrize("dialect", (SqlDialect.SQLITE, SqlDialect.POSTGRES))
def test_datetime_sql_pushdown_is_explicitly_unsupported(
    dialect: SqlDialect,
) -> None:
    expression = Lt(
        op="lt",
        node_id="deadline",
        left=_property(),
        right=LiteralValue(kind="literal", value_type="datetime", value=NOW),
    )
    with pytest.raises(PolicySqlCompileError, match="policy cannot be compiled safely"):
        compile_policy_sql(expression, _schema(), dialect=dialect)


def test_validator_rejects_model_construct_mutation_and_serialization_laundering() -> (
    None
):
    forged = Eq.model_construct(
        op="eq",
        node_id="forged",
        left={"kind": "subject", "attribute": "department"},
        right=_literal("finance"),
    )
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(forged)

    superficially_valid_forgery = Eq.model_construct(
        op="eq",
        node_id="forged-valid",
        left=_subject(),
        right=_literal("finance"),
    )
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(superficially_valid_forgery)

    mutated = Eq(
        op="eq",
        node_id="mutated",
        left=_subject(),
        right=_literal("finance"),
    )
    object.__setattr__(mutated, "left", {"kind": "subject", "attribute": "department"})
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(mutated)

    discriminator_laundering = Eq(
        op="eq",
        node_id="changed-operator",
        left=_subject(),
        right=_literal("finance"),
    )
    object.__setattr__(discriminator_laundering, "op", "neq")
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(discriminator_laundering)

    literal_type_laundering = Eq(
        op="eq",
        node_id="changed-literal-type",
        left=_subject(),
        right=_literal("2026-07-22T08:00:00Z"),
    )
    object.__setattr__(literal_type_laundering.right, "value_type", "datetime")
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(literal_type_laundering)

    polluted = Eq(
        op="eq",
        node_id="polluted",
        left=_subject(),
        right=_literal("finance"),
    )
    object.__setattr__(polluted, "__pydantic_extra__", {"escape": True})
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(polluted)


def test_policy_limits_depth_nodes_strings_collections_and_weight() -> None:
    expression = Eq(
        op="eq",
        node_id="leaf",
        left=_subject(),
        right=_literal("finance"),
    )
    for index in range(20):
        expression = Not(op="not", node_id=f"not-{index}", child=expression)
    validate_policy_expression(expression)
    too_deep = Not(op="not", node_id="not-20", child=expression)
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(too_deep)

    with pytest.raises(ValidationError):
        _literal("x" * 1025)
    with pytest.raises(ValidationError):
        _literal(tuple(str(index) for index in range(257)))

    expensive_children = tuple(
        In(
            op="in",
            node_id=f"in-{index}",
            left=_subject(),
            right=_literal(tuple(f"team-{item}" for item in range(256))),
        )
        for index in range(40)
    )
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(
            All(op="all", node_id="expensive", children=expensive_children)
        )

    oversized_tree = All(
        op="all",
        node_id="oversized-root",
        children=tuple(
            All(
                op="all",
                node_id=f"branch-{branch}",
                children=tuple(
                    Exists(
                        op="exists",
                        node_id=f"leaf-{branch}-{leaf}",
                        value=_subject(),
                    )
                    for leaf in range(256)
                ),
            )
            for branch in range(9)
        ),
    )
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(oversized_tree)


def test_validator_requires_unique_node_ids() -> None:
    expression = All(
        op="all",
        node_id="root",
        children=(
            Exists(op="exists", node_id="duplicate", value=_subject()),
            Exists(op="exists", node_id="duplicate", value=_property()),
        ),
    )
    with pytest.raises(PolicyValidationError, match="policy expression is invalid"):
        validate_policy_expression(expression)


@pytest.mark.parametrize(
    ("expression", "matched"),
    (
        (
            Eq(
                op="eq",
                node_id="eq",
                left=_subject(),
                right=_literal("finance"),
            ),
            True,
        ),
        (
            Neq(
                op="neq",
                node_id="neq",
                left=_subject(),
                right=_literal("engineering"),
            ),
            True,
        ),
        (
            In(
                op="in",
                node_id="in",
                left=_subject(),
                right=_literal(("finance", "legal")),
            ),
            True,
        ),
        (
            Contains(
                op="contains",
                node_id="contains",
                left=_property("tags"),
                right=_literal("approved"),
            ),
            True,
        ),
        (
            Subset(
                op="subset",
                node_id="subset",
                left=_property("tags"),
                right=_literal(("approved", "finance", "audited")),
            ),
            True,
        ),
        (
            Lt(op="lt", node_id="lt", left=_property("rank"), right=_literal(5)),
            True,
        ),
        (
            Gt(op="gt", node_id="gt", left=_property("rank"), right=_literal(3)),
            True,
        ),
        (Exists(op="exists", node_id="exists", value=_subject()), True),
    ),
)
def test_evaluator_supports_closed_comparison_vocabulary(
    expression: object,
    matched: bool,
) -> None:
    assert evaluate_policy(expression, _context()).matched is matched


def test_evaluator_returns_stable_matched_node_ids() -> None:
    expression = All(
        op="all",
        node_id="root",
        children=(
            Eq(
                op="eq",
                node_id="subject",
                left=_subject(),
                right=_literal("finance"),
            ),
            AnyOf(
                op="any",
                node_id="either",
                children=(
                    Eq(
                        op="eq",
                        node_id="resource",
                        left=ResourceRef(kind="resource", attribute="classification"),
                        right=_literal("internal"),
                    ),
                    Eq(
                        op="eq",
                        node_id="application",
                        left=ApplicationRef(
                            kind="application", attribute="environment"
                        ),
                        right=_literal("development"),
                    ),
                ),
            ),
        ),
    )

    result = evaluate_policy(expression, _context())
    assert result == PolicyEvaluationResult(
        matched=True,
        had_error=False,
        matched_node_ids=("subject", "resource", "either", "root"),
    )


def test_missing_and_invalid_values_fail_closed_and_not_cannot_invert_error() -> None:
    missing = Eq(
        op="eq",
        node_id="missing",
        left=_subject("unknown"),
        right=_literal("finance"),
    )
    inverted_missing = Not(op="not", node_id="not-missing", child=missing)
    wrong_type = Lt(
        op="lt",
        node_id="wrong-type",
        left=_subject(),
        right=_literal(5),
    )

    for expression in (missing, inverted_missing, wrong_type):
        result = evaluate_policy(expression, _context())
        assert result.matched is False
        assert result.had_error is True
        assert result.matched_node_ids == ()

    mismatched_neq = Neq(
        op="neq",
        node_id="mismatched-neq",
        left=_subject(),
        right=_literal(7),
    )
    assert evaluate_policy(mismatched_neq, _context()) == PolicyEvaluationResult(
        matched=False,
        had_error=True,
        matched_node_ids=(),
    )

    explicit_exists = Not(
        op="not",
        node_id="not-exists",
        child=Exists(op="exists", node_id="exists", value=_subject("unknown")),
    )
    assert evaluate_policy(explicit_exists, _context()) == PolicyEvaluationResult(
        matched=True,
        had_error=False,
        matched_node_ids=("not-exists",),
    )


def test_any_true_branch_is_decisive_when_another_branch_is_missing() -> None:
    expression = AnyOf(
        op="any",
        node_id="any",
        children=(
            Eq(
                op="eq",
                node_id="classification",
                left=_property(),
                right=_literal("internal"),
            ),
            Eq(
                op="eq",
                node_id="missing-rank",
                left=_property("rank"),
                right=_literal(4),
            ),
        ),
    )
    context = _context(object_properties={("Document", "classification"): "internal"})

    assert evaluate_policy(expression, context) == PolicyEvaluationResult(
        matched=True,
        had_error=False,
        matched_node_ids=("classification", "any"),
    )


def test_runtime_comparison_budget_fails_closed_for_dynamic_collections() -> None:
    collection = tuple(f"item-{index}" for index in range(255)) + ("needle",)
    properties = {("Document", f"tags-{index}"): collection for index in range(40)}
    expression = All(
        op="all",
        node_id="runtime-budget",
        children=tuple(
            Contains(
                op="contains",
                node_id=f"contains-{index}",
                left=_property(f"tags-{index}"),
                right=_literal("needle"),
            )
            for index in range(40)
        ),
    )
    context = _context(object_properties=properties)

    assert evaluate_policy(expression, context) == PolicyEvaluationResult(
        matched=False,
        had_error=True,
        matched_node_ids=(),
    )


def test_evaluator_is_total_for_tainted_inputs() -> None:
    forged = Eq.model_construct(
        op="eq",
        node_id="forged",
        left={"kind": "subject", "attribute": "department"},
        right=_literal("finance"),
    )
    assert evaluate_policy(forged, _context()) == PolicyEvaluationResult(
        matched=False,
        had_error=True,
        matched_node_ids=(),
    )
    assert evaluate_policy("not-an-expression", _context()) == PolicyEvaluationResult(
        matched=False,
        had_error=True,
        matched_node_ids=(),
    )


def test_evaluation_context_defensively_copies_inputs() -> None:
    subject: dict[str, object] = {"department": "finance"}
    context = _context(subject=subject)
    subject["department"] = "engineering"

    expression = Eq(
        op="eq",
        node_id="department",
        left=_subject(),
        right=_literal("finance"),
    )
    assert evaluate_policy(expression, context).matched is True
    with pytest.raises(AttributeError):
        context._subject = {}  # type: ignore[assignment]


def test_policy_integers_are_bounded_to_database_bigint_range() -> None:
    for value in (-(2**63) - 1, 2**63):
        with pytest.raises(ValidationError):
            _literal(value)
        with pytest.raises(ValueError, match="policy evaluation context is invalid"):
            _context(subject={"department": value})


def test_sql_compiler_rejects_non_object_refs_unknown_schema_and_tainted_input() -> (
    None
):
    subject_expression = Eq(
        op="eq",
        node_id="subject",
        left=_subject(),
        right=_literal("finance"),
    )
    unknown_property = Eq(
        op="eq",
        node_id="unknown",
        left=_property("secret"),
        right=_literal("yes"),
    )
    for expression in (subject_expression, unknown_property, "escape"):
        with pytest.raises(
            PolicySqlCompileError, match="policy cannot be compiled safely"
        ):
            compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)

    forged_schema = ObjectSchema.model_construct(definitions=_schema().definitions)
    with pytest.raises(PolicySqlCompileError, match="policy cannot be compiled safely"):
        compile_policy_sql(
            Eq(
                op="eq",
                node_id="classification",
                left=_property(),
                right=_literal("internal"),
            ),
            forged_schema,
            dialect=SqlDialect.SQLITE,
        )


def test_sql_compiler_uses_only_fixed_identifiers_and_bound_values() -> None:
    hostile = 'x") OR 1=1 --'
    schema = ObjectSchema(
        definitions=(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name=hostile,
                value_type=ObjectPropertyType.INTEGER,
            ),
        )
    )
    expression = Eq(
        op="eq",
        node_id="hostile",
        left=_property(hostile),
        right=_literal(7),
    )
    compiled = compile_policy_sql(expression, schema, dialect=SqlDialect.SQLITE)

    assert hostile not in compiled.predicate
    assert compiled.predicate.count("?") == len(compiled.parameters)
    assert any("OR 1=1 --" in str(parameter) for parameter in compiled.parameters)
    assert 7 in compiled.parameters


@given(
    row_value=st.integers(min_value=-1000, max_value=1000),
    expected=st.integers(min_value=-1000, max_value=1000),
    operator=st.sampled_from(("eq", "neq", "lt", "lte", "gt", "gte")),
)
def test_sqlite_compiler_matches_python_evaluator_for_integer_comparisons(
    row_value: int,
    expected: int,
    operator: str,
) -> None:
    node_type = {
        "eq": Eq,
        "neq": Neq,
        "lt": Lt,
        "lte": Lte,
        "gt": Gt,
        "gte": Gte,
    }[operator]
    expression = node_type(
        op=operator,
        node_id="comparison",
        left=_property("rank"),
        right=_literal(expected),
    )
    context = _context(
        object_properties={
            ("Document", "classification"): "internal",
            ("Document", "rank"): row_value,
            ("Document", "tags"): ("finance", "approved"),
        }
    )
    python_result = evaluate_policy(expression, context).matched
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?)",
            ("Document", json.dumps({"rank": row_value})),
        )
        sql_result = connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM objects WHERE {compiled.predicate})",
            compiled.parameters,
        ).fetchone()[0]
    finally:
        connection.close()

    assert bool(sql_result) is python_result


def test_sqlite_compiler_preserves_missing_failure_through_not() -> None:
    expression = Not(
        op="not",
        node_id="not-rank",
        child=Eq(
            op="eq",
            node_id="rank",
            left=_property("rank"),
            right=_literal(4),
        ),
    )
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?)",
            ("Document", json.dumps({"classification": "internal"})),
        )
        matched = connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM objects WHERE {compiled.predicate})",
            compiled.parameters,
        ).fetchone()[0]
    finally:
        connection.close()
    assert matched == 0


def test_sqlite_compiler_allows_decisive_any_when_sibling_is_missing() -> None:
    schema = ObjectSchema(
        definitions=(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="rank",
                value_type=ObjectPropertyType.INTEGER,
            ),
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="missing-rank",
                value_type=ObjectPropertyType.INTEGER,
            ),
        )
    )
    expression = AnyOf(
        op="any",
        node_id="any",
        children=(
            Eq(
                op="eq",
                node_id="rank",
                left=_property("rank"),
                right=_literal(4),
            ),
            Eq(
                op="eq",
                node_id="missing-rank",
                left=_property("missing-rank"),
                right=_literal(4),
            ),
        ),
    )
    compiled = compile_policy_sql(expression, schema, dialect=SqlDialect.SQLITE)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?)",
            ("Document", json.dumps({"rank": 4})),
        )
        matched = connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM objects WHERE {compiled.predicate})",
            compiled.parameters,
        ).fetchone()[0]
    finally:
        connection.close()
    assert matched == 1


@given(
    row_items=st.lists(
        st.integers(min_value=-100, max_value=100),
        max_size=20,
        unique=True,
    ).map(tuple),
    allowed_items=st.lists(
        st.integers(min_value=-100, max_value=100),
        max_size=20,
        unique=True,
    ).map(tuple),
    needle=st.integers(min_value=-100, max_value=100),
)
def test_sqlite_compiler_matches_python_for_collection_operations(
    row_items: tuple[int, ...],
    allowed_items: tuple[int, ...],
    needle: int,
) -> None:
    schema = ObjectSchema(
        definitions=(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="ranks",
                value_type=ObjectPropertyType.INTEGER_SET,
            ),
        )
    )
    expressions = (
        Contains(
            op="contains",
            node_id="contains",
            left=_property("ranks"),
            right=_literal(needle),
        ),
        Subset(
            op="subset",
            node_id="subset",
            left=_property("ranks"),
            right=_literal(allowed_items),
        ),
    )
    context = _context(
        object_properties={
            ("Document", "ranks"): row_items,
        }
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?)",
            ("Document", json.dumps({"ranks": row_items})),
        )
        for expression in expressions:
            expected = evaluate_policy(expression, context).matched
            compiled = compile_policy_sql(
                expression,
                schema,
                dialect=SqlDialect.SQLITE,
            )
            observed = connection.execute(
                f"SELECT EXISTS(SELECT 1 FROM objects WHERE {compiled.predicate})",
                compiled.parameters,
            ).fetchone()[0]
            assert bool(observed) is expected
    finally:
        connection.close()


def test_postgres_compiler_is_parameterized_and_contains_no_input_identifiers() -> None:
    expression = Contains(
        op="contains",
        node_id="tag",
        left=_property("tags"),
        right=_literal("approved"),
    )
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.POSTGRES)

    assert "Document" not in compiled.predicate
    assert "tags" not in compiled.predicate
    assert "approved" not in compiled.predicate
    assert "%s" in compiled.predicate
    assert compiled.parameters
    assert "Document" not in compiled.error_predicate
    assert "tags" not in compiled.error_predicate
    assert "approved" not in compiled.error_predicate
    assert "%s" in compiled.error_predicate
    assert compiled.error_predicate.endswith("IS NULL)")
    assert "COALESCE" not in compiled.error_predicate


@pytest.mark.parametrize(
    "expression",
    (
        Eq(
            op="eq",
            node_id="string-eq",
            left=_property(),
            right=_literal("Ä"),
        ),
        Gt(
            op="gt",
            node_id="string-order",
            left=_property(),
            right=_literal("z"),
        ),
        In(
            op="in",
            node_id="string-in",
            left=_property(),
            right=_literal(("A", "a")),
        ),
        Contains(
            op="contains",
            node_id="string-contains",
            left=_property("tags"),
            right=_literal("Ä"),
        ),
        Subset(
            op="subset",
            node_id="string-subset",
            left=_property("tags"),
            right=_literal(("A", "Ä")),
        ),
    ),
)
def test_postgres_string_sql_uses_explicit_binary_collation(
    expression: object,
) -> None:
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.POSTGRES)
    assert 'COLLATE "C"' in compiled.predicate


@pytest.mark.parametrize(
    "expression",
    (
        Eq(
            op="eq",
            node_id="sqlite-string-eq",
            left=_property(),
            right=_literal("Ä"),
        ),
        Gt(
            op="gt",
            node_id="sqlite-string-order",
            left=_property(),
            right=_literal("z"),
        ),
        In(
            op="in",
            node_id="sqlite-string-in",
            left=_property(),
            right=_literal(("A", "a")),
        ),
        Contains(
            op="contains",
            node_id="sqlite-string-contains",
            left=_property("tags"),
            right=_literal("Ä"),
        ),
        Subset(
            op="subset",
            node_id="sqlite-string-subset",
            left=_property("tags"),
            right=_literal(("A", "Ä")),
        ),
    ),
)
def test_sqlite_string_operators_have_no_partial_collation_fallback(
    expression: object,
) -> None:
    with pytest.raises(PolicySqlCompileError, match="policy cannot be compiled safely"):
        compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)


def test_sqlite_rejects_unicode_string_pushdown_but_python_remains_available() -> None:
    expression = Gt(
        op="gt",
        node_id="unicode-order",
        left=_property(),
        right=_literal("z"),
    )
    context = _context(object_properties={("Document", "classification"): "é"})
    assert evaluate_policy(expression, context).matched is True
    with pytest.raises(PolicySqlCompileError, match="policy cannot be compiled safely"):
        compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)


def test_postgres_rejects_every_numeric_pushdown_shape() -> None:
    scalar_nodes = (Eq, Neq, Lt, Lte, Gt, Gte)
    for value_type, literal in (
        (ObjectPropertyType.INTEGER, 4),
        (ObjectPropertyType.NUMBER, 4.5),
    ):
        schema = ObjectSchema(
            definitions=(
                ObjectPropertyDefinition(
                    object_type="Document",
                    property_name="numeric",
                    value_type=value_type,
                ),
            )
        )
        expressions = tuple(
            node_type(
                op={
                    Eq: "eq",
                    Neq: "neq",
                    Lt: "lt",
                    Lte: "lte",
                    Gt: "gt",
                    Gte: "gte",
                }[node_type],
                node_id=f"numeric-{node_type.__name__}",
                left=_property("numeric"),
                right=_literal(literal),
            )
            for node_type in scalar_nodes
        ) + (
            In(
                op="in",
                node_id="numeric-in",
                left=_property("numeric"),
                right=_literal((literal,)),
            ),
            Exists(
                op="exists",
                node_id="numeric-exists",
                value=_property("numeric"),
            ),
        )
        for expression in expressions:
            with pytest.raises(
                PolicySqlCompileError,
                match="policy cannot be compiled safely",
            ):
                compile_policy_sql(
                    expression,
                    schema,
                    dialect=SqlDialect.POSTGRES,
                )

    for value_type, literal in (
        (ObjectPropertyType.INTEGER_SET, 4),
        (ObjectPropertyType.NUMBER_SET, 4.5),
    ):
        schema = ObjectSchema(
            definitions=(
                ObjectPropertyDefinition(
                    object_type="Document",
                    property_name="numeric_set",
                    value_type=value_type,
                ),
            )
        )
        expressions = (
            Contains(
                op="contains",
                node_id="numeric-contains",
                left=_property("numeric_set"),
                right=_literal(literal),
            ),
            Subset(
                op="subset",
                node_id="numeric-subset",
                left=_property("numeric_set"),
                right=_literal((literal,)),
            ),
            Exists(
                op="exists",
                node_id="numeric-set-exists",
                value=_property("numeric_set"),
            ),
        )
        for expression in expressions:
            with pytest.raises(
                PolicySqlCompileError,
                match="policy cannot be compiled safely",
            ):
                compile_policy_sql(
                    expression,
                    schema,
                    dialect=SqlDialect.POSTGRES,
                )


def test_sqlite_rejects_number_pushdown_for_valid_integer_representation() -> None:
    schema = ObjectSchema(
        definitions=(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="score",
                value_type=ObjectPropertyType.NUMBER,
            ),
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="scores",
                value_type=ObjectPropertyType.NUMBER_SET,
            ),
        )
    )
    exists = Exists(
        op="exists",
        node_id="number-exists",
        value=_property("score"),
    )
    context = _context(
        object_properties={
            ("Document", "score"): 4,
            ("Document", "scores"): (4, 4.5),
        }
    )

    assert evaluate_policy(exists, context).matched is True
    for expression in (
        *tuple(
            node_type(
                op={
                    Eq: "eq",
                    Neq: "neq",
                    Lt: "lt",
                    Lte: "lte",
                    Gt: "gt",
                    Gte: "gte",
                }[node_type],
                node_id=f"number-{node_type.__name__}",
                left=_property("score"),
                right=_literal(4.5),
            )
            for node_type in (Eq, Neq, Lt, Lte, Gt, Gte)
        ),
        In(
            op="in",
            node_id="number-in",
            left=_property("score"),
            right=_literal((4.5,)),
        ),
        exists,
        Contains(
            op="contains",
            node_id="number-contains",
            left=_property("scores"),
            right=_literal(4.5),
        ),
        Subset(
            op="subset",
            node_id="number-subset",
            left=_property("scores"),
            right=_literal((4.5,)),
        ),
        Exists(
            op="exists",
            node_id="number-set-exists",
            value=_property("scores"),
        ),
    ):
        with pytest.raises(
            PolicySqlCompileError,
            match="policy cannot be compiled safely",
        ):
            compile_policy_sql(expression, schema, dialect=SqlDialect.SQLITE)


def test_postgres_safe_types_emit_full_persisted_value_guards() -> None:
    string_exists = compile_policy_sql(
        Exists(op="exists", node_id="string-exists", value=_property()),
        _schema(),
        dialect=SqlDialect.POSTGRES,
    )
    set_exists = compile_policy_sql(
        Exists(op="exists", node_id="set-exists", value=_property("tags")),
        _schema(),
        dialect=SqlDialect.POSTGRES,
    )

    assert "octet_length" in string_exists.predicate
    assert all(
        marker in string_exists.predicate
        for marker in ("chr(1)", "chr(31)", "chr(127)")
    )
    assert "jsonb_array_length" in set_exists.predicate
    assert "<= 256" in set_exists.predicate
    assert "octet_length" in set_exists.predicate
    assert string_exists.predicate.count("%s") == len(string_exists.parameters)
    assert set_exists.predicate.count("%s") == len(set_exists.parameters)


def test_sqlite_rejects_all_string_pushdown_for_json1_utf8_ambiguity() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for payload, path, expected_hex in (
            (r'{"v":"\ud800"}', "$.v", "EDA080"),
            (r'{"v":"\udbff"}', "$.v", "EDAFBF"),
            (r'{"v":"\udc00"}', "$.v", "EDB080"),
            (r'{"v":"\udfff"}', "$.v", "EDBFBF"),
            (r'{"v":"\ud83d\ude00"}', "$.v", "F09F9880"),
            (r'{"v":["\ud800"]}', "$.v[0]", "EDA080"),
            (r'{"v":["\udfff"]}', "$.v[0]", "EDBFBF"),
            (sqlite3.Binary(b'{"v":"\xc0\xaf"}'), "$.v", "C0AF"),
            (sqlite3.Binary(b'{"v":"\xff"}'), "$.v", "FF"),
            (sqlite3.Binary(b'{"v":["\xc0\xaf"]}'), "$.v[0]", "C0AF"),
        ):
            observed = connection.execute(
                "SELECT json_valid(?), json_type(?, ?), "
                "hex(CAST(json_extract(?, ?) AS BLOB))",
                (payload, payload, path, payload, path),
            ).fetchone()
            assert observed == (1, "text", expected_hex)
    finally:
        connection.close()

    scalar_expressions = tuple(
        node_type(
            op={
                Eq: "eq",
                Neq: "neq",
                Lt: "lt",
                Lte: "lte",
                Gt: "gt",
                Gte: "gte",
            }[node_type],
            node_id=f"string-{node_type.__name__}",
            left=_property(),
            right=_literal("internal"),
        )
        for node_type in (Eq, Neq, Lt, Lte, Gt, Gte)
    ) + (
        In(
            op="in",
            node_id="string-in",
            left=_property(),
            right=_literal(("internal",)),
        ),
        Exists(op="exists", node_id="string-exists", value=_property()),
        Not(
            op="not",
            node_id="not-string-exists",
            child=Exists(
                op="exists",
                node_id="not-string-child",
                value=_property(),
            ),
        ),
        All(
            op="all",
            node_id="all-string",
            children=(
                Exists(
                    op="exists",
                    node_id="all-string-child",
                    value=_property(),
                ),
            ),
        ),
        AnyOf(
            op="any",
            node_id="any-string",
            children=(
                Exists(
                    op="exists",
                    node_id="any-string-child",
                    value=_property(),
                ),
            ),
        ),
    )
    set_expressions = (
        Contains(
            op="contains",
            node_id="string-set-contains",
            left=_property("tags"),
            right=_literal("internal"),
        ),
        Subset(
            op="subset",
            node_id="string-set-subset",
            left=_property("tags"),
            right=_literal(("internal",)),
        ),
        Exists(
            op="exists",
            node_id="string-set-exists",
            value=_property("tags"),
        ),
        Not(
            op="not",
            node_id="not-string-set-exists",
            child=Exists(
                op="exists",
                node_id="not-string-set-child",
                value=_property("tags"),
            ),
        ),
        All(
            op="all",
            node_id="all-string-set",
            children=(
                Exists(
                    op="exists",
                    node_id="all-string-set-child",
                    value=_property("tags"),
                ),
            ),
        ),
        AnyOf(
            op="any",
            node_id="any-string-set",
            children=(
                Exists(
                    op="exists",
                    node_id="any-string-set-child",
                    value=_property("tags"),
                ),
            ),
        ),
    )
    for expression in scalar_expressions + set_expressions:
        with pytest.raises(
            PolicySqlCompileError,
            match="policy cannot be compiled safely",
        ):
            compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)


def test_python_evaluator_keeps_valid_unicode_and_rejects_isolated_surrogates() -> None:
    parsed_pair = parse_policy_expression(
        r'{"op":"eq","node_id":"emoji","left":{"kind":"object_property",'
        r'"object_type":"Document","property_name":"classification"},'
        r'"right":{"kind":"literal","value":"\ud83d\ude00"}}'
    )
    for value in ("中文", "😀"):
        context = _context(
            object_properties={
                ("Document", "classification"): value,
                ("Document", "tags"): (value,),
            }
        )
        exists = Exists(op="exists", node_id="unicode-exists", value=_property())
        set_exists = Exists(
            op="exists",
            node_id="unicode-set-exists",
            value=_property("tags"),
        )
        assert evaluate_policy(exists, context).matched is True
        assert evaluate_policy(set_exists, context).matched is True
    assert (
        evaluate_policy(
            parsed_pair,
            _context(object_properties={("Document", "classification"): "😀"}),
        ).matched
        is True
    )

    for invalid in ("\ud800", "\udbff", "\udc00", "\udfff"):
        with pytest.raises(ValueError, match="policy evaluation context is invalid"):
            _context(object_properties={("Document", "classification"): invalid})


@pytest.mark.parametrize(
    "properties",
    (
        {"classification": "x" * 1025},
        {"classification": "é" * 513},
        {"classification": "bad\u0001value"},
        {"classification": "bad\u007fvalue"},
        {"classification": 7},
    ),
)
def test_sqlite_string_exists_is_rejected_before_inspecting_row_shape(
    properties: dict[str, object],
) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute(
            "SELECT json_valid(?)",
            (json.dumps(properties),),
        ).fetchone() == (1,)
    finally:
        connection.close()
    with pytest.raises(PolicySqlCompileError, match="policy cannot be compiled safely"):
        compile_policy_sql(
            Exists(op="exists", node_id="string-row", value=_property()),
            _schema(),
            dialect=SqlDialect.SQLITE,
        )


def test_sqlite_valid_string_boundary_and_missing_rows_do_not_enable_pushdown() -> None:
    for expression in (
        Exists(op="exists", node_id="valid-string", value=_property()),
        Not(
            op="not",
            node_id="missing-string",
            child=Exists(
                op="exists",
                node_id="missing-string-child",
                value=_property(),
            ),
        ),
    ):
        with pytest.raises(
            PolicySqlCompileError,
            match="policy cannot be compiled safely",
        ):
            compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)


@pytest.mark.parametrize(
    "properties",
    (
        {"tags": tuple(f"tag-{index}" for index in range(257))},
        {"tags": ("x" * 1025,)},
        {"tags": ("bad\u0002value",)},
        {"tags": "not-an-array"},
        {"tags": (True,)},
    ),
)
def test_sqlite_string_set_exists_is_rejected_before_inspecting_row_shape(
    properties: dict[str, object],
) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute(
            "SELECT json_valid(?)",
            (json.dumps(properties),),
        ).fetchone() == (1,)
    finally:
        connection.close()
    with pytest.raises(PolicySqlCompileError, match="policy cannot be compiled safely"):
        compile_policy_sql(
            Exists(
                op="exists",
                node_id="string-set-row",
                value=_property("tags"),
            ),
            _schema(),
            dialect=SqlDialect.SQLITE,
        )


def test_sqlite_integer_boundaries_fail_closed_without_cast_errors() -> None:
    invalid_rows = (
        (_schema(), _property("rank"), {"rank": 2**63}),
        (_schema(), _property("rank"), {"rank": -(2**63) - 1}),
        (_schema(), _property("rank"), {"rank": 1.5}),
        (_schema(), _property("rank"), '{"rank":1e999}'),
        (_schema(), _property("rank"), '{"rank":-1e999}'),
    )
    for schema, reference, properties in invalid_rows:
        expression = Not(
            op="not",
            node_id="not-exists-number",
            child=Exists(
                op="exists",
                node_id="exists-number",
                value=reference,
            ),
        )
        assert (
            _sqlite_row_matches(
                expression.child,
                schema,
                properties,
            )
            is False
        )
        assert _sqlite_row_matches(expression, schema, properties) is False


def test_sqlite_integer_set_exists_rejects_out_of_range_members() -> None:
    schema = ObjectSchema(
        definitions=(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name="ranks",
                value_type=ObjectPropertyType.INTEGER_SET,
            ),
        )
    )
    exists = Exists(
        op="exists",
        node_id="integer-set-exists",
        value=_property("ranks"),
    )
    not_exists = Not(op="not", node_id="not-integer-set-exists", child=exists)

    for properties in ({"ranks": (2**63,)}, '{"ranks":[1e999]}'):
        assert _sqlite_row_matches(exists, schema, properties) is False
        assert _sqlite_row_matches(not_exists, schema, properties) is False


def test_sql_compiler_rejects_dynamic_collection_worst_case_over_budget() -> None:
    contains_expression = All(
        op="all",
        node_id="contains-budget",
        children=tuple(
            Contains(
                op="contains",
                node_id=f"contains-{index}",
                left=_property(f"tags-{index}"),
                right=_literal(index),
            )
            for index in range(40)
        ),
    )
    contains_schema = ObjectSchema(
        definitions=tuple(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name=f"tags-{index}",
                value_type=ObjectPropertyType.INTEGER_SET,
            )
            for index in range(40)
        )
    )
    subset_expression = All(
        op="all",
        node_id="subset-budget",
        children=tuple(
            Subset(
                op="subset",
                node_id=f"subset-{index}",
                left=_property(f"subset-{index}"),
                right=_literal(tuple(range(256))),
            )
            for index in range(20)
        ),
    )
    subset_schema = ObjectSchema(
        definitions=tuple(
            ObjectPropertyDefinition(
                object_type="Document",
                property_name=f"subset-{index}",
                value_type=ObjectPropertyType.INTEGER_SET,
            )
            for index in range(20)
        )
    )

    for expression, schema in (
        (contains_expression, contains_schema),
        (subset_expression, subset_schema),
    ):
        with pytest.raises(
            PolicySqlCompileError,
            match="policy cannot be compiled safely",
        ):
            compile_policy_sql(expression, schema, dialect=SqlDialect.SQLITE)


def test_sqlite_integer_schema_rejects_decimal_row_without_raising() -> None:
    expression = Eq(
        op="eq",
        node_id="integer",
        left=_property("rank"),
        right=_literal(4),
    )
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?)",
            ("Document", json.dumps({"rank": 1.2})),
        )
        matched = connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM objects WHERE {compiled.predicate})",
            compiled.parameters,
        ).fetchone()[0]
    finally:
        connection.close()
    assert matched == 0


@pytest.mark.parametrize(
    ("depth", "maximum_bytes", "maximum_parameters"),
    (
        (5, 40_000, 256),
        (10, 900_000, 8_192),
    ),
)
def test_balanced_sql_compilation_growth_is_linear_and_bounded(
    depth: int,
    maximum_bytes: int,
    maximum_parameters: int,
) -> None:
    def balanced(level: int, path: str) -> object:
        if level == 0:
            return Eq(
                op="eq",
                node_id=f"leaf-{path}",
                left=_property(),
                right=_literal("internal"),
            )
        return AnyOf(
            op="any",
            node_id=f"any-{path}",
            children=(
                balanced(level - 1, f"{path}0"),
                balanced(level - 1, f"{path}1"),
            ),
        )

    compiled = compile_policy_sql(
        balanced(depth, "r"),
        _schema(),
        dialect=SqlDialect.POSTGRES,
    )

    assert len(compiled.predicate.encode("utf-8")) < maximum_bytes
    assert len(compiled.parameters) < maximum_parameters


def test_compile_rejects_non_require_polarities_at_compile_time() -> None:
    from eios.authz.policy import PolicyEffect

    expression = Eq(
        op="eq",
        node_id="polarity",
        left=_property("rank"),
        right=_literal(5),
    )
    for effect in (PolicyEffect.DENY_IF, PolicyEffect.OBLIGATION_IF):
        with pytest.raises(
            PolicySqlCompileError,
            match="polarity is not supported by row-filter SQL compilation",
        ):
            compile_policy_sql(
                expression,
                _schema(),
                dialect=SqlDialect.SQLITE,
                effect=effect,
            )
    # Effect must be the exact enum: strings and other values fail closed.
    with pytest.raises(PolicySqlCompileError):
        compile_policy_sql(
            expression,
            _schema(),
            dialect=SqlDialect.SQLITE,
            effect="REQUIRE",  # type: ignore[arg-type]
        )
    explicit = compile_policy_sql(
        expression,
        _schema(),
        dialect=SqlDialect.SQLITE,
        effect=PolicyEffect.REQUIRE,
    )
    implicit = compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)
    assert explicit == implicit


def test_sqlite_error_predicate_flags_exactly_evaluation_errors() -> None:
    expression = Eq(
        op="eq",
        node_id="rank-eq",
        left=_property("rank"),
        right=_literal(5),
    )
    rows = (
        ("match", {"rank": 5}),
        ("no-match", {"rank": 3}),
        ("wrong-type", {"rank": "high"}),
        ("missing", {}),
    )
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (row_id TEXT, object_type TEXT, object_properties TEXT)"
        )
        for row_id, properties in rows:
            connection.execute(
                "INSERT INTO objects VALUES (?, ?, ?)",
                (row_id, "Document", json.dumps(properties)),
            )
        matched = {
            observed[0]
            for observed in connection.execute(
                f"SELECT row_id FROM objects WHERE {compiled.predicate}",
                compiled.parameters,
            )
        }
        errored = {
            observed[0]
            for observed in connection.execute(
                f"SELECT row_id FROM objects WHERE {compiled.error_predicate}",
                compiled.parameters,
            )
        }
    finally:
        connection.close()
    assert matched == {"match"}
    assert errored == {"wrong-type", "missing"}
    # The error channel agrees with the fail-closed in-memory evaluator.
    for row_id, properties in rows:
        object_properties = (
            {("Document", "rank"): properties["rank"]} if properties else {}
        )
        evaluation = evaluate_policy(
            expression, _context(object_properties=object_properties)
        )
        assert evaluation.had_error is (row_id in errored)
        assert evaluation.matched is (row_id in matched)


def test_deny_if_row_semantics_stay_fail_closed_via_error_channel() -> None:
    """A DENY_IF consumer must union the error channel; NOT predicate alone
    would let error rows escape denial. The compiled plan makes the safe
    composition possible and the entrypoint rejects DENY_IF outright."""

    expression = Eq(
        op="eq",
        node_id="rank-eq",
        left=_property("rank"),
        right=_literal(5),
    )
    compiled = compile_policy_sql(expression, _schema(), dialect=SqlDialect.SQLITE)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE objects (row_id TEXT, object_type TEXT, object_properties TEXT)"
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?, ?)",
            ("error-row", "Document", json.dumps({"rank": "high"})),
        )
        naive_denied = connection.execute(
            f"SELECT count(*) FROM objects WHERE {compiled.predicate}",
            compiled.parameters,
        ).fetchone()[0]
        must_deny = connection.execute(
            "SELECT count(*) FROM objects WHERE "
            f"({compiled.predicate}) OR ({compiled.error_predicate})",
            compiled.parameters + compiled.parameters,
        ).fetchone()[0]
    finally:
        connection.close()
    # The folded predicate alone would treat the error row as "not matched"
    # (fail-open under DENY_IF); the error channel restores must-deny.
    assert naive_denied == 0
    assert must_deny == 1
