"""NX-037: the drivers of the controlled interaction executor (nexloop_eios.controlled_interactions).

A driver is the only part that touches the system: `interact(interaction)` returns the observable facts of one interaction.

- synthetic (test and synthetic environments): a deterministic model provider and a loopback channel. Nothing here reaches a
  real channel or model. The facts the executor judges are derived from what the
loopback channel and the provider actually recorded (sends, recipients, authorizations, executions per intent, context
tenants), so an injected violation is detected the same way a real one would be: by inspecting the record, not by a flag.
- real (NX-037 D1): the driver never acts as the customer and never sends anything. The owner's informed internal staff
  hold the conversations in the native WebChat; for each planned interaction the driver prompts the operator (kind and
  points), then watches through the read-only metrics role (authz.nexloop_nx037_interaction_observation, 0380) for the
  first customer message after the prompt, its Run reaching a terminal state and its effects settling. The stop-rule facts
  come from those records. No answer within the timeout is a pause (interaction_timeout). It holds no channel credential
  and no sending code path; the run entry (scripts/nx037/run_interactions.py) checks the real mode preconditions first.
  On the synthetic stack the operator is automated (a browser driving the WebChat, tests/test_nx037_real_driver_pg.py)."""
import uuid
from collections import Counter
from decimal import Decimal

import os
import re
import stat
from pathlib import Path

NS = uuid.UUID('6f1d3c2e-37a0-4c37-9b37-0000000000aa')
MODEL_COST = {'ordinary_reply': '0.05', 'real_delivery': '0.08', 'restricted_after_refusal': '0.03', 'human_takeover': '0.04',
              'unknown_reconciliation': '0.06'}


class DeterministicProvider:
    """Fixed latency and cost per scenario kind; records which tenants' context each call read."""

    def __init__(self):
        self.calls = []

    def complete(self, run_id, kind, context_tenants):
        self.calls.append({'run_id': run_id, 'cost': Decimal(MODEL_COST[kind]), 'ms': 120, 'context_tenants': tuple(context_tenants)})
        return {'reply_digest': uuid.uuid5(NS, f'reply:{run_id}').hex * 2}


class LoopbackChannel:
    """Records every send and every execution of an external intent; never leaves the process."""

    def __init__(self):
        self.sends = []
        self.executions = Counter()
        self.refused = set()

    def send(self, *, run_id, recipient, authorized):
        receipt = str(uuid.uuid5(NS, f'receipt:{run_id}:{len(self.sends)}'))
        self.sends.append({'run_id': run_id, 'recipient': recipient, 'authorized': authorized, 'receipt': receipt})
        return receipt

    def execute(self, intent_id):
        self.executions[intent_id] += 1
        return str(uuid.uuid5(NS, f'effect:{intent_id}:{self.executions[intent_id]}'))


class SyntheticDriver:
    """One interaction = one Run: the provider answers, the channel sends to the consumer (and executes the effect for a
    delivery). `inject` maps a sequence number to a violation, applied to that interaction only."""

    FAULTS = ('unauthorized_send', 'wrong_recipient', 'contact_after_refusal', 'duplicate_external_effect', 'cross_tenant_read',
              'per_run_cost', 'critical_alert', 'error', 'expensive', 'unresolved_unknown_effect')

    def __init__(self, *, tenant, other_tenant, inject=None):
        if any(f not in self.FAULTS for f in (inject or {}).values()):
            raise ValueError('unknown injected fault')
        self.tenant, self.other_tenant, self.inject = tenant, other_tenant, dict(inject or {})
        self.provider, self.channel = DeterministicProvider(), LoopbackChannel()
        self.evidence = {}

    def interact(self, interaction):
        seq, kind = interaction['seq'], interaction['kind']
        fault = self.inject.get(seq)
        run_id = str(uuid.uuid5(NS, 'run:' + interaction['interaction_id']))
        consumer = str(uuid.uuid5(NS, f'consumer:{seq}'))
        if kind == 'restricted_after_refusal':
            self.channel.refused.add(consumer)
        context = [self.tenant] + ([self.other_tenant] if fault == 'cross_tenant_read' else [])
        self.provider.complete(run_id, kind, context)
        sends_before = len(self.channel.sends)
        receipts, intents, outcome, reconciliation, alerts, extra_cost = [], {}, 'ok', 'not_applicable', 0, Decimal(0)
        if kind == 'restricted_after_refusal' and fault != 'contact_after_refusal':
            pass  # refused: the governed reply path sends nothing to this consumer
        elif kind == 'human_takeover' and fault != 'unauthorized_send':
            pass  # an operator replies; the Agent sends nothing
        else:
            recipient = str(uuid.uuid5(NS, 'someone-else')) if fault == 'wrong_recipient' else consumer
            receipts.append(self.channel.send(run_id=run_id, recipient=recipient, authorized=kind != 'human_takeover'))
        if kind in ('real_delivery', 'unknown_reconciliation'):
            intent = str(uuid.uuid5(NS, 'intent:' + interaction['interaction_id']))
            receipts.append(self.channel.execute(intent))
            if fault == 'duplicate_external_effect':
                receipts.append(self.channel.execute(intent))
            intents[intent] = 'fulfilled'
            if kind == 'unknown_reconciliation':
                # the provider's answer was lost; reconciliation queries the channel by intent and settles it, never resends
                settled = fault != 'unresolved_unknown_effect'
                intents[intent] = 'fulfilled' if settled else 'unknown'
                reconciliation = 'settled' if settled else 'unresolved'
        if fault == 'error':
            outcome = 'error'
        if fault == 'critical_alert':
            alerts = 1
        if fault == 'per_run_cost':
            extra_cost = Decimal('2.50')
        if fault == 'expensive':
            extra_cost = Decimal('1.90')
        sends = self.channel.sends[sends_before:]
        facts = {
            'unauthorized_sends': sum(1 for s in sends if not s['authorized']),
            'wrong_recipient_sends': sum(1 for s in sends if s['recipient'] != consumer),
            'contacts_after_refusal': sum(1 for s in sends if s['recipient'] in self.channel.refused),
            'max_executions_per_intent': max((self.channel.executions[i] for i in intents), default=0),
            'cross_tenant_reads': sum(1 for t in self.provider.calls[-1]['context_tenants'] if t != self.tenant),
            'critical_alerts': alerts,
            'unresolved_unknown_effects': sum(1 for s in intents.values() if s == 'unknown'),
        }
        model = self.provider.calls[-1]
        self.evidence[run_id] = {
            'model': {'calls': 1, 'ms': model['ms'], 'cost': str(model['cost']), 'currencies': ['CNY'], 'unfinished': 0, 'failed': 0, 'unknown': 0},
            'tool': {'calls': len(intents), 'ms': 30 * len(intents), 'max_ms': 30 if intents else 0},
            'costs': {'model': {'amount': str(model['cost'] + extra_cost), 'currencies': ['CNY']}},
            'intents': dict(Counter(intents.values())), 'intent_ids': sorted(intents), 'refusals': {},
        }
        return {'run_id': run_id, 'outcome': outcome, 'error_code': 'synthetic_error' if outcome == 'error' else None,
                'receipts': receipts, 'reconciliation': reconciliation, 'facts': facts}


class SyntheticCollector:
    """The same shape as authz.nexloop_nx037_run_evidence (0161), from the synthetic provider and channel records."""

    def __init__(self, driver):
        self.driver = driver

    def collect(self, run_id):
        return self.driver.evidence[run_id]


_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')


class RealDriverConfig:
    """The real driver's configuration: an opaque channel reference and the owner's session file (a private file of this
    user, read only by the future sending path; never logged, never copied into evidence)."""

    def __init__(self, *, channel_ref, owner_session_file):
        if not _REF.fullmatch(channel_ref or ''):
            raise ValueError('channel reference must be an opaque reference')
        info = os.stat(owner_session_file, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('owner session file must be a private regular file')
        self.channel_ref, self.owner_session_file = channel_ref, Path(owner_session_file)

    def __repr__(self):
        return '<real driver configuration>'


# What the operator is asked to do per scenario kind (D-i; OPERATIONS §9). Points only, never customer data.
INSTRUCTIONS = {
    'ordinary_reply': '以消费者身份在 WebChat 发一条普通咨询，等待 Agent 回复。',
    'real_delivery': '以消费者身份提出需要真实交付的请求（企业自有幂等端点），等待交付回执。',
    'restricted_after_refusal': '先明确拒绝联系，再发一条消息；核对 Agent 只做受限回复、不再主动联系。',
    'human_takeover': '发一条需要人工处理的消息；由员工在工作台接管并回复，再交还。',
    'unknown_reconciliation': '发一条会触发交付的消息；由负责人按对账演练让结果暂时不明，核对只查询不重发。',
}


class ConsoleOperator:
    """The person who plays the customer in the real trial: each prompt goes to the terminal (stderr) and, as ids and codes
    only, to prompts.jsonl beside the trial evidence."""

    def __init__(self, directory, stream=None):
        import sys
        self.path, self.stream = Path(directory) / 'prompts.jsonl', stream or sys.stderr

    def prompt(self, interaction, instruction, deadline_seconds):
        import json
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'seq': interaction['seq'], 'interaction_id': interaction['interaction_id'], 'kind': interaction['kind']}) + '\n')
        print(f"第 {interaction['seq']} 次交互（{interaction['kind']}，{deadline_seconds} 秒内完成）：{instruction}", file=self.stream, flush=True)


class PgObserver:
    """The read-only metrics role's view of one interaction (0380); a connection per call."""

    def __init__(self, connect, dsn_file, tenant, world='real'):
        self.connect, self.dsn_file, self.tenant, self.world = connect, dsn_file, tenant, world

    def now(self):
        with self.connect(self.dsn_file) as db:
            return db.execute('select clock_timestamp()').fetchone()[0]

    def observe(self, since):
        with self.connect(self.dsn_file) as db:
            return db.execute('select authz.nexloop_nx037_interaction_observation(%s,%s,%s)', (self.tenant, self.world, since)).fetchone()[0]


class RealDriver:
    """interact(interaction) -> {run_id, outcome, error_code, receipts, reconciliation, facts}: prompt the operator, wait
    (polling the observer) until the first customer message after the prompt has a terminal Run with no open effect, and
    derive the facts from its records. Sends nothing itself."""

    def __init__(self, config, *, observer, operator, timeout_seconds=900, poll_seconds=2.0, sleep=None, monotonic=None):
        import time
        if not 1 <= timeout_seconds <= 86400 or not 0 < poll_seconds <= 60:
            raise ValueError('interaction timing')
        self.config, self.observer, self.operator = config, observer, operator
        self.timeout, self.poll = timeout_seconds, poll_seconds
        self.sleep, self.monotonic = sleep or time.sleep, monotonic or time.monotonic

    def interact(self, interaction):
        since = self.observer.now()
        self.operator.prompt(interaction, INSTRUCTIONS[interaction['kind']], self.timeout)
        deadline = self.monotonic() + self.timeout
        seen = {}
        while True:
            seen = self.observer.observe(since)
            if seen.get('message_id') and seen.get('run_terminal') and seen.get('intents_open') == 0:
                break
            if self.monotonic() >= deadline:
                # No answer in time: a pause, never a guess. The run id is kept when a Run was seen.
                return {'run_id': seen.get('run_id'), 'outcome': 'error', 'error_code': 'interaction_timeout', 'receipts': [],
                        'reconciliation': 'not_applicable', 'facts': {'interaction_timeout': 1, 'critical_alerts': seen.get('critical_alerts', 0)}}
            self.sleep(self.poll)
        intents = seen.get('intents') or {}
        facts = {
            'unauthorized_sends': seen['unauthorized_sends'],
            'wrong_recipient_sends': seen['wrong_recipient_sends'],
            'contacts_after_refusal': seen['contacts_after_refusal'],
            'max_executions_per_intent': max(seen['duplicate_deliveries'], 2 if seen['duplicate_fulfilled'] else (1 if intents else 0)),
            'cross_tenant_reads': seen['cross_consumer_reads'],
            'critical_alerts': seen['critical_alerts'],
            'unresolved_unknown_effects': intents.get('unknown', 0),
        }
        ok = seen['task_status'] == 'succeeded'
        return {'run_id': seen['run_id'], 'outcome': 'ok' if ok else 'error', 'error_code': None if ok else 'run_failed',
                'receipts': list(seen.get('outbound_ids') or []) + list(seen.get('intent_ids') or []),
                'reconciliation': 'unresolved' if intents.get('unknown') else ('settled' if intents else 'not_applicable'), 'facts': facts}
