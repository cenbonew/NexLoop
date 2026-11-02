"""NX-064 (L6): since the Host reads a thrown tool's code (pi-runtime-adapter toolCode), a failed Run's failure_detail can be any code a
tool ends with, and failure_tool any effect tool. Each must pass the evidence pack's vocabulary (scripts/nx037/evidence_pack.py), or the
pack would drop it as an unknown value. Read from the Host source, so a new code or tool without a vocabulary entry fails here."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/nx037'))
import evidence_pack as E  # noqa: E402

TOOLS = (ROOT / 'apps/agent-host/src/runtime-effect-tools.ts').read_text()
ADAPTER = (ROOT / 'apps/agent-host/src/pi-runtime-adapter.ts').read_text()
# Codes that never reach a Run's record: the two internal ambiguity markers (always turned into the unavailable code) and a Host
# configuration error (the Host refuses to start).
INTERNAL = {'runtime_effect_ambiguous', 'incentive_ambiguous', 'runtime_effect_configuration_invalid'}


def codes():
    found = set(re.findall(r"RuntimeError\('([a-z_]+)'\)", TOOLS)) | set(re.findall(r"code:'([a-z_]+)'", TOOLS)) | set(re.findall(r"fail\('([a-z_]+)'\)", TOOLS))
    found |= set(re.findall(r"'([a-z_]+)'", re.search(r'OUTREACH_CODES=\[([^\]]*)\]', TOOLS).group(1)))
    for listed in re.findall(r"\[((?:'[a-z_]+',?)+)\]\.includes\((?:error|denied)\.code\)", TOOLS):  # the codes passed through as they are
        found |= set(re.findall(r"'([a-z_]+)'", listed))
    found |= set(re.findall(r"'(runtime_tool_[a-z_]+)'", ADAPTER))  # the gateway's code for a thrown tool and its refusals
    return found - INTERNAL


def test_every_code_a_tool_ends_with_is_in_the_vocabulary():
    found = codes()
    assert {'runtime_effect_unavailable', 'incentive_unavailable', 'incentive_payload_conflict', 'runtime_tool_unavailable', 'tool_error'} - {'tool_error'} <= found
    assert sorted(c for c in found | {'tool_error'} if not E.allowed(c)) == []


def test_every_effect_tool_is_in_the_vocabulary():
    tools = set(re.findall(r"defineTool\(\{name:'(nexloop\.[a-z._]+)'", TOOLS))
    assert {'nexloop.enterprise.deliver', 'nexloop.incentive.issue', 'nexloop.outreach.send', 'nexloop.plan.outcome'} <= tools
    assert sorted(t for t in tools if not E.allowed(t)) == []


def test_text_outside_the_vocabulary_is_still_dropped():
    assert not E.allowed('Synthetic failure with text') and not E.allowed('incentive_unavailable_extra')
