"""NX-037 prep, synthetic environment only: a deterministic model provider and a loopback channel for the controlled
interaction executor. Nothing here reaches a real channel or model. The facts the executor judges are derived from what the
loopback channel and the provider actually recorded (sends, recipients, authorizations, executions per intent, context
tenants), so an injected violation is detected the same way a real one would be: by inspecting the record, not by a flag."""
import uuid
from collections import Counter
from decimal import Decimal

from support.tenants import TENANT_A, TENANT_OTHER

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

    def __init__(self, *, tenant=TENANT_A, inject=None):
        self.tenant, self.inject = tenant, dict(inject or {})
        self.provider, self.channel = DeterministicProvider(), LoopbackChannel()
        self.evidence = {}

    def interact(self, interaction):
        seq, kind = interaction['seq'], interaction['kind']
        fault = self.inject.get(seq)
        run_id = str(uuid.uuid5(NS, 'run:' + interaction['interaction_id']))
        consumer = str(uuid.uuid5(NS, f'consumer:{seq}'))
        if kind == 'restricted_after_refusal':
            self.channel.refused.add(consumer)
        context = [self.tenant] + ([TENANT_OTHER] if fault == 'cross_tenant_read' else [])
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
    """The same shape as authz.nexloop_nx037_run_evidence (0280), from the synthetic provider and channel records."""

    def __init__(self, driver):
        self.driver = driver

    def collect(self, run_id):
        return self.driver.evidence[run_id]
