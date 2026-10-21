"""A guard against copying a stale function body (NX-041 integration: twice a migration re-created a function from an older body and
silently undid another branch's fix - S6c's stand-in submit and S7b's entry in the Host context migration).

Every migration from a base on that re-creates an existing function (`create or replace function`, also inside a DO block's
EXECUTE) is compared with that function's latest earlier definition, in catalog order, following renames and drops:
- the header by meaning (parameters with names and types, return type, language, volatility, SECURITY DEFINER, every SET), so a body
  re-exported with pg_get_functiondef ("RETURNS jsonb LANGUAGE plpgsql ... SET search_path TO 'pg_catalog'") compares equal to the
  hand-written form; the function's own name is not part of it (a function made by a rename keeps the definition it was created with);
- the body with comments and all whitespace removed.
The difference must be exactly what the registry allows for that migration and function: the registered reverts, applied to the
new body, must give back the earlier body, and the header may differ only by the registered header change. An unregistered
redefinition, or one that differs beyond what is registered, is reported. A body copied from an older version shows as a
difference beyond the reverts.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / 'packages/eios-core/src/eios/migrations'

_FUNC = r'([a-z_]+\.[a-z0-9_]+)\s*\(([^)]*)\)'
_CREATE = re.compile(r'create\s+(or\s+replace\s+)?function\s+' + _FUNC, re.I)
_RENAME = re.compile(r'alter\s+function\s+' + _FUNC + r'\s+rename\s+to\s+([a-z0-9_]+)', re.I)
_DROP = re.compile(r'drop\s+function\s+(?:if\s+exists\s+)?' + _FUNC, re.I)
_TYPES = {'text', 'jsonb', 'json', 'integer', 'int', 'uuid', 'bigint', 'boolean', 'numeric', 'timestamptz', 'bytea', 'regclass', 'date', 'smallint',
          'interval', 'record', 'regprocedure'}


def signature(args: str) -> str:
    """Argument types only (names, modes and defaults dropped), lower case: the identity of an overload."""
    out = []
    for part in [p.strip() for p in re.split(r',(?![^(]*\))', args) if p.strip()]:
        part = re.split(r'\s+default\s+|\s*:=\s*', part, flags=re.I)[0].strip().lower()
        words = part.split()
        if words and words[0] in ('in', 'out', 'inout', 'variadic'):
            words = words[1:]
        if len(words) >= 2 and ' '.join(words[1:]) and (words[0] not in _TYPES or words[1] in _TYPES or '.' in words[1] or words[1].endswith('[]')):
            words = words[1:]
        out.append(' '.join(words).replace('timestamp with time zone', 'timestamptz'))
    return ','.join(out)


@dataclass
class Definition:
    migration: str
    name: str
    text: str  # from the argument list to the end of the statement (the function's own name excluded)

    @property
    def parts(self) -> tuple[str, str, str]:
        """(argument list, header attributes before and after the body, body)."""
        depth, i = 0, 0
        for i, ch in enumerate(self.text):
            depth += ch == '('
            depth -= ch == ')'
            if depth == 0:
                break
        args, rest = self.text[1:i], self.text[i + 1:]
        m = re.compile(r'\bas\s+(\$[A-Za-z_]*\$)', re.I).search(rest)
        end = rest.index(m.group(1), m.end())
        return args, rest[:m.start()] + ' ' + rest[end + len(m.group(1)):], rest[m.end():end]

    @property
    def header(self) -> dict:
        args, attrs, _ = self.parts
        return canonical_header(args, attrs)

    @property
    def body(self) -> str:
        return self.parts[2]


@dataclass
class Redefinition:
    migration: str  # file name
    stem: str  # file name without its (temporary) number
    name: str
    sig: str
    new: Definition
    old: Definition


_ATTR_WORDS = ('language', 'stable', 'immutable', 'volatile', 'security', 'set', 'strict', 'parallel', 'cost', 'called', 'leakproof', 'rows', 'window')


def _type(text: str) -> str:
    return re.sub(r'\s+', '', text.lower().replace('timestamp with time zone', 'timestamptz').replace('character varying', 'varchar'))


def canonical_header(args: str, attrs: str) -> dict:
    """The meaning of a function header, independent of layout and of pg_get_functiondef's spelling."""
    params = []
    for part in [p.strip() for p in re.split(r',(?![^(]*\))', args) if p.strip()]:
        words = part.split(None, 1)
        params.append((words[0].lower(), _type(words[1])) if len(words) == 2 and words[0].lower() not in _TYPES else ('', _type(part)))
    text = re.sub(r'\s+', ' ', attrs.lower().replace('"', '').replace("'", '')).strip().rstrip(';').strip()
    sets = {}
    for m in re.finditer(r'\bset\s+([a-z_]+)\s*(?:=|\bto\b)\s*([a-z0-9_.,\s]+?)(?=\s+(?:' + '|'.join(_ATTR_WORDS) + r')\b|$)', text):
        sets[m.group(1)] = [v.strip() for v in m.group(2).split(',')]
    returns = re.search(r'\breturns\s+(.+?)(?=\s+(?:' + '|'.join(_ATTR_WORDS) + r')\b|$)', text)
    volatility = next((v for v in ('immutable', 'stable', 'volatile') if re.search(rf'\b{v}\b', text)), 'volatile')
    language = re.search(r'\blanguage\s+([a-z0-9_]+)', text)
    return {'params': params, 'returns': _type(returns.group(1)) if returns else None, 'language': language.group(1) if language else None,
            'volatility': volatility, 'security_definer': bool(re.search(r'\bsecurity\s+definer\b', text)), 'sets': sets,
            'strict': bool(re.search(r'\bstrict\b|returns null on null input', text))}


def _statement_end(source: str, start: int) -> tuple[int, str]:
    """End of a function statement starting at `start`: after its dollar-quoted body and the attributes up to ';' or the end of an
    enclosing EXECUTE string. Returns (end, body)."""
    m = re.compile(r'\bas\s+(\$[A-Za-z_]*\$)', re.I).search(source, start)
    tag = m.group(1)
    body_end = source.index(tag, m.end())
    rest = source[body_end + len(tag):]
    stop = re.search(r';|\$[A-Za-z_]*\$', rest)
    return body_end + len(tag) + (stop.start() if stop else len(rest)), source[m.end():body_end]


def normalize(text: str) -> str:
    """Comments and every whitespace character removed (the comparison is on tokens, not layout)."""
    text = re.sub(r'--[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'\s+', '', text)


def migrations(directory: Path = MIGRATIONS) -> list[Path]:
    catalog = json.loads((directory / 'catalog.json').read_text())['migrations']
    return [directory / m['name'] for m in catalog]


def redefinitions(files: list[Path], base: str = '0259') -> list[Redefinition]:
    """Every re-creation of an existing function in a migration numbered `base` or later, with the definition it replaces."""
    state: dict[tuple[str, str], Definition] = {}
    found = []
    for path in files:
        source = path.read_text()
        events = [(m.start(), 'create', m) for m in _CREATE.finditer(source)]
        events += [(m.start(), 'rename', m) for m in _RENAME.finditer(source)]
        events += [(m.start(), 'drop', m) for m in _DROP.finditer(source)]
        for _, kind, m in sorted(events, key=lambda e: e[0]):
            key = (m.group(2 if kind == 'create' else 1).lower(), signature(m.group(3 if kind == 'create' else 2)))
            if kind == 'create':
                end, _ = _statement_end(source, m.end())
                definition = Definition(path.name, key[0], source[m.start(3) - 1:end])
                if key in state and path.name[:4] >= base:
                    found.append(Redefinition(path.name, path.name[5:].removesuffix('.sql'), key[0], key[1], definition, state[key]))
                state[key] = definition
            elif kind == 'rename':
                if key in state:
                    state[(key[0].split('.')[0] + '.' + m.group(3).lower(), key[1])] = state.pop(key)
            else:
                state.pop(key, None)
    return found


@dataclass(frozen=True)
class Allowed:
    """What a registered redefinition may change: `reverts` are (pattern, replacement) applied to the new body in order; the result,
    normalized, must equal the earlier body normalized; `header` is the allowed header change {key: (earlier, new)} (a SET item as
    'set.<name>'). `why` says what the change is."""
    stem: str
    name: str
    why: str
    reverts: tuple[tuple[str, str], ...] = ()
    header: tuple[tuple[str, object, object], ...] = ()


def check(found: list[Redefinition], registry: list[Allowed]) -> list[str]:
    """Problems: unregistered redefinitions, differences beyond the registered reverts, and registry entries matching nothing."""
    problems, used = [], set()
    by_key = {(a.stem, a.name): a for a in registry}
    for r in found:
        allowed = by_key.get((r.stem, r.name))
        if allowed is None:
            problems.append(f'{r.migration}: {r.name}({r.sig}) redefines the definition of {r.old.migration} without a registry entry')
            continue
        used.add((r.stem, r.name))
        reverted = r.new.body
        for pattern, replacement in allowed.reverts:
            reverted, n = re.subn(pattern, replacement, reverted, flags=re.S)
            if n == 0:
                problems.append(f'{r.migration}: {r.name}: the registered change {pattern!r} is not there')
        new_header, old_header = r.new.header, r.old.header
        for key, before, after in allowed.header:
            target = new_header['sets'] if key.startswith('set.') else new_header
            field = key.removeprefix('set.')
            if target.get(field) != after:
                problems.append(f'{r.migration}: {r.name}: header {key} is {target.get(field)!r}, registered {after!r}')
            target[field] = before
        if new_header != old_header:
            problems.append(f'{r.migration}: {r.name}({r.sig}) header differs from {r.old.migration}: {old_header} -> {r.new.header}')
        if normalize(reverted) != normalize(r.old.body):
            problems.append(f'{r.migration}: {r.name}({r.sig}) differs from {r.old.migration} beyond "{allowed.why}"\n   '
                            + first_difference(reverted, r.old.body))
    problems += [f'registry entry {stem} {name} matches no redefinition' for stem, name in sorted(set(by_key) - used)]
    return problems


def first_difference(a: str, b: str, width: int = 120) -> str:
    """Where two normalized texts first differ (for a readable failure)."""
    a, b = normalize(a), normalize(b)
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return f'...{a[max(0, i - 40):i + width]}\n   vs ...{b[max(0, i - 40):i + width]}'
