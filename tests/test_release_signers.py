"""O1 (owner 2026-10-12): the release signing public key and allowed_signers are committed; the private key stays outside the repository (check_publication rejects key material)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'deploy/release'


def test_allowed_signers_names_only_the_committed_release_key_for_the_release_namespace():
    key_type, key, comment = (RELEASE / 'nexloop-release.pub').read_text().split()
    assert key_type == 'ssh-ed25519' and comment == 'nexloop-release'
    lines = [line for line in (RELEASE / 'allowed_signers').read_text().splitlines() if line.strip() and not line.startswith('#')]
    assert lines == [f'nexloop-release namespaces="nexloop-release" {key_type} {key}']

