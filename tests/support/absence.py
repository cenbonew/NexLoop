"""Absence checks that a random digest or id cannot fool (no-hex-like-absence-markers).

`'9000' not in response.text` fails by chance whenever a random sha256 or uuid in the same text happens to contain the marker.
`absent(marker, value)` walks the parsed JSON instead: a number leaf must not equal the marker; a string leaf must not contain it,
except a leaf that is wholly a hex digest, a uuid or a hex-and-separator id (it carries no seeded value, only randomness)."""
import json
import re

_HEXLIKE = re.compile(r'[0-9a-fA-F:_.-]+')


def leaves(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from leaves(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from leaves(v)
    else:
        yield value


def absent(marker, value):
    """True when `marker` (str or number) appears nowhere in the parsed JSON `value` (a str is parsed first)."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    text = str(marker)
    for leaf in leaves(value):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, (int, float)):
            if str(leaf) == text or (isinstance(marker, (int, float)) and leaf == marker):
                return False
        elif isinstance(leaf, str) and text in leaf and not (_HEXLIKE.fullmatch(leaf) and leaf != text):
            return False
    return True
