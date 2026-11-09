"""NX-062 M1 (docs/implementation/NX-062-design.md §2.2, L6 C2, owner Q-B/Q-F 2026-11-09): machine key format, the one-HMAC
digest under the stored pepper version, the private pepper file, and that no key material reaches repr, str or exceptions.
Keys are generated at run time; no key literal appears in this file (prepush-scan gate 3 blocks the nxm_ shape)."""
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from nexloop_eios import machine_keys as M

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import history_audit as H  # noqa: E402


def pepper_text(*versions):
    return ''.join(f'{v} {secrets.token_hex(32)}\n' for v in versions)


@pytest.fixture
def peppers():
    return M.parse_peppers('# synthetic\n\n' + pepper_text(1, 2))


def test_generated_keys_round_trip_and_are_distinct():
    keys = [M.generate() for _ in range(64)]
    assert len({k.prefix_id for k in keys}) == 64 and len({k.secret for k in keys}) == 64
    for key in keys:
        assert len(key.token) == M.KEY_LENGTH and M.KEY.fullmatch(key.token)
        assert M.parse(key.token) == key


@pytest.mark.parametrize('mutate', [
    lambda t: t[:-1], lambda t: t + 'A', lambda t: 'nxk_' + t[4:], lambda t: t.upper(),
    lambda t: t[:16] + '-' + t[17:], lambda t: t[:4] + '0' + t[5:], lambda t: t[:20] + '=' + t[21:],
    lambda t: ' ' + t[1:], lambda t: t.replace('_', '.', 1)])
def test_anything_but_one_exact_key_is_refused(mutate):
    token = M.generate().token
    with pytest.raises(M.MachineKeyInvalid) as error:
        M.parse(mutate(token))
    assert token[17:] not in str(error.value)


@pytest.mark.parametrize('value', [None, 7, b'x', '', 'Bearer x'])
def test_non_strings_and_short_values_are_refused(value):
    with pytest.raises(M.MachineKeyInvalid):
        M.parse(value)


def test_digest_is_bound_to_version_prefix_and_secret(peppers):
    key = M.generate()
    one = M.digest(key, peppers, 1)
    assert len(one) == 32 and M.digest(key, peppers, 1) == one
    assert M.digest(key, peppers, 2) != one
    assert M.digest(M.MachineKey(M.generate().prefix_id, key.secret), peppers, 1) != one
    assert M.digest(M.MachineKey(key.prefix_id, M.generate().secret), peppers, 1) != one
    assert M.matches(key, peppers, 1, one) and M.matches(key, peppers, 1, memoryview(one))
    assert not M.matches(key, peppers, 2, one) and not M.matches(key, peppers, 1, one.hex())


def test_rotation_old_version_still_verifies_new_keys_use_current(peppers):
    key = M.generate()
    stored = M.digest(key, peppers, 1)
    rotated = M.parse_peppers(pepper_text(3) + '\n'.join(f'{v} {peppers.material(v).hex()}' for v in peppers.versions))
    assert rotated.current == 3 and M.matches(key, rotated, 1, stored)
    retired = M.parse_peppers('\n'.join(f'{v} {rotated.material(v).hex()}' for v in (2, 3)))
    with pytest.raises(M.MachineKeyInvalid):
        M.matches(key, retired, 1, stored)


def test_unknown_version_fails_closed(peppers):
    with pytest.raises(M.MachineKeyInvalid):
        M.digest(M.generate(), peppers, 9)


@pytest.mark.parametrize('text', ['', '# only a comment\n', '1 ' + 'ab' * 31, '0 ' + 'ab' * 32, '1 ' + 'AB' * 32,
                                  '1 ' + 'ab' * 32 + ' extra', 'x ' + 'ab' * 32])
def test_malformed_pepper_files_are_refused(text):
    with pytest.raises(M.MachineKeyInvalid):
        M.parse_peppers(text)


def test_duplicate_version_and_reused_material_are_refused():
    material = secrets.token_hex(32)
    with pytest.raises(M.MachineKeyInvalid, match='duplicated'):
        M.parse_peppers(f'1 {material}\n1 {secrets.token_hex(32)}\n')
    with pytest.raises(M.MachineKeyInvalid, match='reused'):
        M.parse_peppers(f'1 {material}\n2 {material}\n')


def test_private_pepper_file_mode_and_errors_hold_no_material(tmp_path):
    text = pepper_text(1)
    path = tmp_path / 'machine_key_peppers'
    path.write_text(text)
    path.chmod(0o600)
    assert M.load_peppers(path).versions == (1,)
    path.chmod(0o640)
    with pytest.raises(M.MachineKeyInvalid) as error:
        M.load_peppers(path)
    assert text.split()[1] not in str(error.value) and error.value.__cause__ is None and error.value.__suppress_context__
    link = tmp_path / 'link'
    path.chmod(0o600)
    os.symlink(path, link)
    with pytest.raises(M.MachineKeyInvalid):
        M.load_peppers(link)
    with pytest.raises(M.MachineKeyInvalid):
        M.load_peppers(tmp_path / 'missing')


def test_no_material_in_repr_or_str(peppers):
    key = M.generate()
    for shown in (repr(key), str(key), f'{key}', repr([key])):
        assert key.secret not in shown and '<redacted>' in shown and key.prefix_id in shown
    for shown in (repr(peppers), str(peppers), f'{peppers}'):
        assert shown == 'Peppers(versions=(1, 2))'


def test_prepush_gate_three_and_history_audit_match_a_generated_key_only():
    expression = H.credential_expression()
    token = M.generate().token
    assert expression.search(token.encode())
    assert not expression.search(b'nxm_' + b'a' * 12 + b'_short')
    assert re.search(r'nxm_\[a-z2-7\]\{12\}_\[A-Za-z0-9_-\]\{43\}', (ROOT / 'scripts/ci/prepush-scan.sh').read_text())


def test_this_module_and_tests_hold_no_key_literal():
    expression = H.credential_expression()
    for path in (ROOT / 'packages/eios-core/src/nexloop_eios/machine_keys.py', Path(__file__)):
        assert not expression.search(path.read_bytes()), path


def test_gitleaks_default_rules_are_unchanged_by_m1():
    # M1 adds the nxm_ shape to prepush gate 3 (NX-060 forbids custom gitleaks rules); the configuration checker still passes.
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/check_gitleaks_config.py'), str(ROOT / '.gitleaks.toml'),
                             str(ROOT / 'versions.lock.json')], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
