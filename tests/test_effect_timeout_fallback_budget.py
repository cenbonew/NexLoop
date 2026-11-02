"""NX-064 acceptance (design v2 §4, acceptance 3): the effect limit's upper bound fits the reply fallback Run. A tool call takes at most
two requests (the one resend of the same body), each within EFFECT_TIMEOUT_MAX_MS; the fallback Run's tool calls at that bound end
within its active_timeout_seconds, and every published reply-guarantee policy still loads (its check start_after_seconds + the fallback
Run's longest duration < reply_window_seconds does not depend on the effect limit). Reads the Host constants from its source."""
import re
from pathlib import Path

import pytest

from nexloop_eios.contact_restrictions import fallback_duration, load_reply_policy

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'apps/agent-host/src/runtime-effect-tools.ts').read_text()
POLICIES = sorted((ROOT / 'deploy/configuration').glob('reply-guarantee.v*.json'))


def constant(name):
    return int(re.search(rf'export const {name}=(\d+);', SOURCE).group(1))


def test_the_host_bounds_are_the_designed_ones():
    assert (constant('EFFECT_TIMEOUT_MS'), constant('EFFECT_TIMEOUT_MIN_MS'), constant('EFFECT_TIMEOUT_MAX_MS')) == (2000, 1000, 5000)
    # One resend at most: the effect call and the incentive tool each send the same body once more, never a third time.
    # (the first request and the resend: two sends each).
    assert SOURCE.count("return await timedRequest(metric,()=>this.once(url,ca,key,body,valid,operation,arguments_));") == 2
    assert SOURCE.count('return await send();') == 2


@pytest.mark.parametrize('path', POLICIES, ids=lambda p: p.name)
def test_the_fallback_run_holds_its_tool_calls_at_the_upper_limit_and_the_policy_loads(path):
    policy = load_reply_policy(path)
    fallback = policy['fallback']
    worst = fallback['run_budget']['maximum_tool_calls'] * 2 * constant('EFFECT_TIMEOUT_MAX_MS') / 1000
    assert worst < fallback['run_budget']['active_timeout_seconds'], (path.name, worst)
    assert fallback['start_after_seconds'] + fallback_duration(policy) < policy['reply_window_seconds']
