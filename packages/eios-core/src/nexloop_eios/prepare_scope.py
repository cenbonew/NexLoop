"""ADR-029a: reuse of Source-signed proofs within one context preparation of one inbound message.

The relay prepares a message Run's context in three signed steps (the SQL core snapshot, the first bind, the bind recheck after
the write). Each step used to rebuild the same Source-signed proofs: the bind Action proof, the Artifact proofs, the Run proofs,
the Message READ envelope and the service catalog envelope with its two object READ envelopes. A PrepareScope holds them for one
preparation only (one message, one Run, one tenant and world: the producer's own session), and the snapshot's catalog value
read uses the very same object READ envelopes.

A proof is reused only while every expires_at in it is at least MARGIN ahead; a proof without a readable expiry is never kept.
SQL verifies each use against the current facts, records and catalog digests exactly as for a fresh proof; a refused use fails
the preparation (no retry inside it), and the next preparation builds everything anew. Nothing outlives the preparation."""
from datetime import UTC, datetime, timedelta
import json

MARGIN = timedelta(seconds=5)


def expiry(value):
    """The earliest expires_at anywhere in a proof (nested dicts, lists and JSON object texts); None when there is none."""
    found = []

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key == 'expires_at' and isinstance(child, str):
                    try:
                        found.append(datetime.fromisoformat(child))
                    except ValueError:
                        pass
                else:
                    walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str) and item.startswith('{'):
            try:
                walk(json.loads(item))
            except ValueError:
                pass
    walk(value)
    aware = [t if t.tzinfo else t.replace(tzinfo=UTC) for t in found]
    return min(aware) if aware else None


class PrepareScope:
    """Proofs of one context preparation, keyed by what they prove."""

    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.items, self.built, self.reused = {}, 0, 0

    def get(self, key, build):
        held = self.items.get(key)
        if held is not None and held[1] - self.clock() >= MARGIN:
            self.reused += 1
            return held[0]
        self.items.pop(key, None)
        value = build()
        self.built += 1
        expires = expiry(value)
        if expires is not None and expires - self.clock() >= MARGIN:
            self.items[key] = (value, expires)
        return value
