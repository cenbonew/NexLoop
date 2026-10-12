"""NX-031: the exact-type fast paths of eios.authz.facts._canonical_value give exactly the upstream result (and digest) for every
value kind it accepts, including subclasses that must still take the upstream chain. The reference below is the upstream
function as vendored at 1e363db, verbatim."""
import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from json import dumps

import pytest
from pydantic import BaseModel

from eios.authz import facts
from eios.authz.operations import Operation


def upstream(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if type(value) is bytes:
        return {"$bytes_hex": value.hex()}
    if isinstance(value, BaseModel):
        return upstream(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): upstream(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, frozenset):
        canonical = tuple(upstream(item) for item in value)
        return sorted(canonical, key=lambda item: dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    if isinstance(value, (tuple, list)):
        return [upstream(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise TypeError("authorization fact contains a non-canonical value")


class Colour(str, Enum):
    RED = 'red'


class Number(int, Enum):
    ONE = 1


class Inner(BaseModel):
    name: str
    when: datetime
    tags: frozenset[str]


class StrSub(str):
    pass


def leaves(rng):
    return [rng.choice(['a', 'β', '', 'x' * 30]), rng.randint(-5, 10**12), rng.random(), True, False, None, Colour.RED, Number.ONE,
            Operation.READ, datetime(2026, 10, 12, 8, tzinfo=timezone(timedelta(hours=8))), b'\x00\xff',
            frozenset({'b', 'a', 'c'}), Inner(name='n', when=datetime(2026, 1, 1, tzinfo=UTC), tags=frozenset({'y', 'x'}))]


def build(rng, depth):
    if depth == 0:
        return rng.choice(leaves(rng))
    kind = rng.randrange(4)
    if kind == 0:
        return {rng.choice(['k', 'a', 'z', 1, 'B']): build(rng, depth - 1) for _ in range(rng.randint(0, 4))}
    if kind == 1:
        return [build(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    if kind == 2:
        return tuple(build(rng, depth - 1) for _ in range(rng.randint(0, 3)))
    return rng.choice(leaves(rng))


@pytest.mark.parametrize('seed', range(40))
def test_fast_path_equals_upstream_on_random_structures(seed):
    rng = random.Random(seed)
    for _ in range(50):
        value = build(rng, 4)
        assert facts._canonical_value(value) == upstream(value)
        assert facts._canonical_digest(value) == __import__('hashlib').sha256(
            dumps(upstream(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def test_subclasses_and_rejections_keep_the_upstream_behaviour():
    for value in (Colour.RED, Number.ONE, {'k': Colour.RED}):
        assert facts._canonical_value(value) == upstream(value)
    for bad in (object(), {'k': object()}, [set([1])], StrSub('s'), {'k': StrSub('v')}):  # upstream: exact primitive types only
        with pytest.raises(TypeError):
            facts._canonical_value(bad)
        with pytest.raises(TypeError):
            upstream(bad)
