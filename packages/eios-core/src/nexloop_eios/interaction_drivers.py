"""NX-037: the drivers of the controlled interaction executor (nexloop_eios.controlled_interactions).

A driver is the only part that touches the system: `interact(interaction)` returns the observable facts of one interaction.

- synthetic (test and synthetic environments): a deterministic model provider and a loopback channel. Nothing here reaches a
  real channel or model. The facts the executor judges are derived from what the
loopback channel and the provider actually recorded (sends, recipients, authorizations, executions per intent, context
tenants), so an injected violation is detected the same way a real one would be: by inspecting the record, not by a flag.
- real (NX-037 D1): the driver never acts as the customer and never sends anything. The owner's informed internal staff
  hold the conversations in the native WebChat; for each planned interaction the driver prompts the operator (kind and
  points), then watches through the read-only metrics role (authz.nexloop_nx037_interaction_observation, 0172) for the
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


# Plan v3 variants (owner 2026-10-14): what the agent playing the customer (or the owner's staff side) adds to the kind's points.
VARIANTS = {
    'single_question': '只问一个问题。',
    'long_conversation': '进行 6–10 轮的长对话（每轮等 Agent 回复后再发下一条），全部发完后再标记完成。',
    'vague_need': '先给出模糊的需求，等 Agent 澄清后再补充具体信息。',
    'direct_order': '直接、明确地提出交付请求。',
    'vague_then_order': '先模糊描述，澄清后再明确提出交付请求。',
    'single_refusal': '明确拒绝一次后结束。',
    'repeated_refusal': '拒绝联系后再来信，并再次明确拒绝。',
    'refusal_then_question': '拒绝联系后再来信问一个问题（核对 Agent 只回复这条来信）。',
    'takeover_reply': '员工在工作台接管，回复后交还。',
    'takeover_after_delivery': '先完成一次交付请求，再由员工接管；员工在参考端点标记工单后交还。',
    'post_delayed': '负责人先执行端点演练 --delay-next-posts 1，再发会触发交付的消息。',
    'get_unavailable': '负责人先执行端点演练 --get-unavailable-seconds，再发会触发交付的消息。',
}


def instruction(interaction):
    """The kind's points, then the plan v3 variant and the consumer account to use (points only, never customer data)."""
    text = INSTRUCTIONS[interaction['kind']]
    if interaction.get('variant'):
        text += ' 变体 ' + interaction['variant'] + '：' + VARIANTS.get(interaction['variant'], '按计划说明执行。')
    if interaction.get('consumer'):
        text += ' 使用消费者账号 ' + interaction['consumer'] + '。'
    return text


DELIVER_ACTION = 'nexloop.enterprise.deliver'
SETTLED_STATES = ('fulfilled', 'confirmed', 'failed')
# The kinds whose effect evidence the runner checks (0203); the others are judged by their Run and stop facts as before.
EFFECT_EVIDENCE = ('ordinary_reply', 'real_delivery', 'unknown_reconciliation')


class ConsoleOperator:
    """The person who plays the customer in the real trial: each prompt goes to the terminal (stderr) and, as ids and codes
    only, to prompts.jsonl beside the trial evidence."""

    def __init__(self, directory, stream=None, *, wait=False, sleep=None, monotonic=None, wall=None):
        import sys
        import time
        self.path, self.stream = Path(directory) / 'prompts.jsonl', stream or sys.stderr
        # Plan v3 (multi-turn): with wait, the prompt returns only when the operator (the agent playing the customer) marks the
        # interaction done by creating <dir>/operator-done/<seq>, or at its deadline; every turn then belongs to it.
        self.done, self.wait = Path(directory) / 'operator-done', wait
        self.sleep, self.monotonic, self.wall = sleep or time.sleep, monotonic or time.monotonic, wall or time.time

    def prompt(self, interaction, instruction, deadline_seconds):
        import json
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        done = self.done / str(interaction['seq'])
        issued = self.wall()
        # 10/18 (trial 4f5d795c, interaction 3 redone after a void): a done marker older than this prompt belongs to an earlier
        # attempt. It is retired (renamed, never deleted) and never counts as this interaction's completion.
        if done.exists() and done.stat().st_mtime < issued:
            done.rename(done.with_name(f"{done.name}.stale-{int(issued)}"))
        # One machine-readable line per prompt for the agent playing the customer: what to play, with which account, and (when
        # waiting) the file to create once every message of the interaction is sent.
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'seq': interaction['seq'], 'interaction_id': interaction['interaction_id'], 'kind': interaction['kind'],
                                     **({'variant': interaction['variant']} if interaction.get('variant') else {}),
                                     **({'consumer': interaction['consumer']} if interaction.get('consumer') else {}),
                                     **({'done_file': str(done), 'deadline_seconds': deadline_seconds} if self.wait else {})}) + '\n')
        tail = f'全部发完后创建 {done} 表示完成。' if self.wait else ''
        print(f"第 {interaction['seq']} 次交互（{interaction['kind']}，{deadline_seconds} 秒内完成）：{instruction}{tail}", file=self.stream, flush=True)
        if self.wait:
            self.done.mkdir(mode=0o700, parents=True, exist_ok=True)
            end = self.monotonic() + deadline_seconds
            fresh = lambda: done.exists() and done.stat().st_mtime >= issued  # noqa: E731
            while not fresh() and self.monotonic() < end:
                self.sleep(1)
            # 10/17 calibration: the deadline without the done marker is not a completed interaction (interaction_timeout).
            return fresh()
        return None


class PgObserver:
    """The read-only metrics role's view of one interaction (0172); a connection per call."""

    def __init__(self, connect, dsn_file, tenant, world='real'):
        self.connect, self.dsn_file, self.tenant, self.world = connect, dsn_file, tenant, world

    def now(self):
        with self.connect(self.dsn_file) as db:
            return db.execute('select clock_timestamp()').fetchone()[0]

    def observe(self, since):
        with self.connect(self.dsn_file) as db:
            return db.execute('select authz.nexloop_nx037_interaction_observation(%s,%s,%s)', (self.tenant, self.world, since)).fetchone()[0]

    def messages(self, since):
        """The customer messages accepted since the prompt, in order (0196): [{message_id, accepted_at}]."""
        with self.connect(self.dsn_file) as db:
            return db.execute('select authz.nexloop_nx037_interaction_messages(%s,%s,%s)', (self.tenant, self.world, since)).fetchone()[0]


class RealDriver:
    """interact(interaction) -> {run_id, run_ids, messages, outcome, error_code, receipts, reconciliation, facts}: prompt the
    operator, wait (polling the observer) until every customer message accepted since the prompt (0196; a long conversation
    has several) is settled, and derive the facts from all their records: a failed Run in any turn makes the interaction an
    error, contacts after refusal and every other stop fact are summed over the turns, and each turn's Run is collected for
    cost. An observer without `messages` (tests) gives the first message only. Sends nothing itself."""

    def __init__(self, config, *, observer, operator, timeout_seconds=900, poll_seconds=2.0, sleep=None, monotonic=None, consumers=None):
        import time
        if not 1 <= timeout_seconds <= 86400 or not 0 < poll_seconds <= 60:
            raise ValueError('interaction timing')
        self.config, self.observer, self.operator = config, observer, operator
        self.timeout, self.poll = timeout_seconds, poll_seconds
        # 10/18 (trial 4f5d795c): the planned customer accounts; a message from any other account is never part of an interaction.
        self.consumers = frozenset(str(c).removeprefix('consumer:') for c in (consumers or ()))
        self.sleep, self.monotonic = sleep or time.sleep, monotonic or time.monotonic

    def interact(self, interaction):
        since = self.observer.now()
        # One deadline for the whole interaction, the operator's wait included (10/17 calibration: the operator waited the full
        # timeout, then a fresh deadline started and the turns seen so far were judged).
        deadline = self.monotonic() + self.timeout
        completed = self.operator.prompt(interaction, instruction(interaction), self.timeout)
        multi = hasattr(self.observer, 'messages')
        # The account each message must come from: the interaction's own consumer (plan v3), else any planned account.
        allowed = frozenset([str(interaction['consumer']).removeprefix('consumer:')]) if interaction.get('consumer') else self.consumers
        views, offplan = [], 0
        while True:
            views, offplan = self._views(since, allowed) if multi else ([self.observer.observe(since)], 0)
            if completed is not False and (views or offplan) and all(self._settled(v) for v in views):
                break
            if completed is False or self.monotonic() >= deadline:
                # No completion marker, or no answer in time: a pause, never a guess. The run id is kept when a Run was seen.
                first = views[0] if views else {}
                return {'run_id': first.get('run_id'), 'outcome': 'error', 'error_code': 'interaction_timeout', 'receipts': [],
                        'reconciliation': 'not_applicable',
                        'facts': {'interaction_timeout': 1, 'critical_alerts': max((v.get('critical_alerts', 0) for v in views), default=0)}}
            self.sleep(self.poll)
        if offplan:
            # A message from an account outside the plan: counted, the interaction is an error (never silently dropped).
            out = self._result(views, multi, interaction['kind']) if views else {'run_id': None, 'outcome': 'error', 'receipts': [],
                'reconciliation': 'not_applicable', 'facts': {}, **({'run_ids': [], 'messages': 0} if multi else {})}
            out.update(outcome='error', error_code='offplan_sender')
            out['facts'] = {**out['facts'], 'offplan_messages': offplan}
            return out
        return self._result(views, multi, interaction['kind'])

    def _views(self, since, allowed=frozenset()):
        """Every customer message of the interaction, each through the 0172 observation at its own acceptance time, and how many
        came from an account outside `allowed` (0207 consumer_id; a message without one cannot be verified and counts too)."""
        views, offplan = [], 0
        for message in self.observer.messages(since) or []:
            if allowed and message.get('consumer_id') not in allowed:
                offplan += 1
                continue
            seen = self.observer.observe(message['accepted_at'])
            if seen.get('message_id') != message['message_id']:
                seen = {'message_id': None}  # two messages accepted at the same instant: not settled until the next look
            views.append(seen)
        return views, offplan

    @staticmethod
    def _settled(seen):
        """A message is settled when its Run is terminal with no open effect, or, with no Run (the conversation was taken over),
        once it has been answered (the staff reply bound to it)."""
        if not seen.get('message_id'):
            return False
        if seen.get('run_id'):
            return bool(seen.get('run_terminal')) and seen.get('intents_open') == 0
        return bool(seen.get('outbound_ids'))

    @staticmethod
    def _effect_gap(kind, views):
        """The effect evidence each kind requires (0203 `effects`), or the error code naming what is missing: real_delivery needs
        an enterprise delivery fulfilled; unknown_reconciliation one that went through unknown and was settled by the query;
        ordinary_reply none at all. A view without `effects` is no evidence."""
        if any('effects' not in v for v in views if v.get('run_id')):
            return 'effect_evidence_unavailable' if kind in EFFECT_EVIDENCE else None
        deliveries = [v['effects'].get(DELIVER_ACTION) for v in views if v.get('run_id')]
        states, seen = {}, 0
        for d in deliveries:
            for state, n in ((d or {}).get('states') or {}).items():
                states[state] = states.get(state, 0) + n
            seen += (d or {}).get('unknown_seen', 0)
        settled = sum(n for state, n in states.items() if state in SETTLED_STATES)
        if kind == 'real_delivery' and not any(states.get(state) for state in ('fulfilled', 'confirmed')):
            return 'delivery_missing'
        if kind == 'unknown_reconciliation' and not (seen and settled and settled == sum(states.values())):
            return 'reconciliation_missing'
        if kind == 'ordinary_reply' and states:
            return 'unexpected_delivery'
        return None

    @staticmethod
    def _result(views, multi, kind=None):
        intents = {}
        for v in views:
            for state, n in (v.get('intents') or {}).items():
                intents[state] = intents.get(state, 0) + n
        facts = {
            'unauthorized_sends': sum(v['unauthorized_sends'] for v in views),
            'wrong_recipient_sends': sum(v['wrong_recipient_sends'] for v in views),
            'contacts_after_refusal': sum(v['contacts_after_refusal'] for v in views),
            'max_executions_per_intent': max(max(v['duplicate_deliveries'], 2 if v['duplicate_fulfilled'] else (1 if v.get('intents') else 0)) for v in views),
            'cross_tenant_reads': sum(v['cross_consumer_reads'] for v in views),
            'critical_alerts': max(v['critical_alerts'] for v in views),
            'unresolved_unknown_effects': intents.get('unknown', 0),
        }
        runs = [v for v in views if v.get('run_id')]
        ok = bool(runs) and all(v['task_status'] == 'succeeded' for v in runs)
        code = None if ok else 'run_failed'
        gap = RealDriver._effect_gap(kind, views) if kind in EFFECT_EVIDENCE else None
        if ok and gap:
            # The Run succeeded but the kind's effect evidence is missing: never ok, and the trial pauses for the owner.
            ok, code = False, gap
            facts['effect_evidence_missing'] = 1
        out = {'run_id': runs[0]['run_id'] if runs else None, 'outcome': 'ok' if ok else 'error', 'error_code': code,
               'receipts': [r for v in views for r in list(v.get('outbound_ids') or []) + list(v.get('intent_ids') or [])],
               'reconciliation': 'unresolved' if intents.get('unknown') else ('settled' if intents else 'not_applicable'), 'facts': facts}
        if multi:
            # Every turn's Run belongs to the interaction: the executor collects each one's evidence (cost, model, tools).
            out.update(run_ids=[v['run_id'] for v in runs], messages=len(views))
        return out
