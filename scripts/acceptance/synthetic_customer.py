#!/usr/bin/env python3
"""Synthetic customers speak in the WebChat (S6 synthetic interactions, NX-043 X7 S3/O2 real inbound): each turn of a private plan
is one message of one simulated_agent / synthetic account, through the same consumer path a person uses (nexloop_eios.consumer_channel:
login, conversation, message with CSRF and Idempotency-Key, logout). It observes nothing else; run_interactions.py --driver real (or
the X7 checks) observe the Runs and effects.

    security find-generic-password ... | uv run --frozen python scripts/acceptance/synthetic_customer.py --config CONFIG --plan PLAN

Passwords come only from stdin, one `alias<TAB>password` line per account of the plan (never argv, output, log or exception text);
run the script file, never `python -` (stdin is the password channel). Configuration (private JSON, 0600, owned):
    {"schema": "nexloop-synthetic-customer/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>",
     "reader_dsn_file": "<READ_ONLY_DSN_FILE_0600>", "accounts": {"customer-1": "<USERNAME>", ...}}
Plan (private JSON, 0600, owned): {"schema": "nexloop-synthetic-customer-plan/1", "plan_id": "s6-20261022-1",
     "turns": [{"account": "customer-1", "body": "<what the customer says>", "wait_reply_seconds": 120}, ...]}   (1-200 turns)

Before any message (preflight): every account logs in once (429: back off by Retry-After) and its Consumer is checked read only
(reader_dsn_file, set read only and verified on connect: control.nexloop_consumer_owners -> control.nexloop_consumer_personas).
Any account whose Consumer is not simulated_agent / synthetic refuses the whole run (nothing is sent); one being erased (or erased)
is reported conversation_unavailable and skipped. Each turn: login, the account's latest conversation or a new one (403:
conversation_unavailable, the account is skipped from then on), the message with Idempotency-Key <plan_id>-<turn>-<alias> (rerunning
the plan replays, never repeats), optionally the wait for a reply (polling; timeout is reported, never resent), logout.
Output: one JSON line per turn {turn, account (alias), status, http, replied, elapsed_ms} and a summary line; no body, id, username,
cookie, token or password. Exit: 0 every turn sent or replayed; 1 a turn was not; 2 input refused or preflight refused.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nexloop_eios.consumer_channel import ConsumerChannelError, ConsumerClient  # noqa: E402
import s6_common as C  # noqa: E402

CONFIG_SCHEMA, PLAN_SCHEMA = 'nexloop-synthetic-customer/1', 'nexloop-synthetic-customer-plan/1'
ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
PLAN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
SYNTHETIC = ('simulated_agent', 'synthetic')
LOGIN_TRIES, MAX_BACKOFF = 5, 120


class Refused(ValueError):
    pass


def _json(path, schema):
    try:
        value = json.loads(C.read_private(path))
    except (C.Refused, ValueError) as error:
        raise Refused(f'{Path(path).name}: {error if isinstance(error, C.Refused) else "not JSON"}') from None
    if type(value) is not dict or value.get('schema') != schema:
        raise Refused(f'schema {schema} required')
    return value


def load(config_path, plan_path):
    config, plan = _json(config_path, CONFIG_SCHEMA), _json(plan_path, PLAN_SCHEMA)
    accounts = config.get('accounts')
    if type(accounts) is not dict or not accounts or any(not ALIAS.fullmatch(a) or type(u) is not str or not u for a, u in accounts.items()):
        raise Refused('configuration accounts: {alias: username}')
    for key in ('origin', 'reader_dsn_file'):
        if type(config.get(key)) is not str or not config[key]:
            raise Refused(f'configuration {key} required')
    turns = plan.get('turns')
    if type(plan.get('plan_id')) is not str or not PLAN_ID.fullmatch(plan['plan_id']):
        raise Refused('plan_id: [A-Za-z0-9][A-Za-z0-9._-]{0,63}')
    if type(turns) is not list or not 1 <= len(turns) <= 200:
        raise Refused('turns: 1-200')
    for t in turns:
        if (type(t) is not dict or set(t) - {'account', 'body', 'wait_reply_seconds'} or t.get('account') not in accounts
                or type(t.get('body')) is not str or not t['body'].strip() or len(t['body']) > 8192
                or type(t.get('wait_reply_seconds', 0)) not in (int, float) or not 0 <= t.get('wait_reply_seconds', 0) <= 900):
            raise Refused('turn: {account (configured alias), body (1-8192), wait_reply_seconds 0-900}')
    return config, plan


def read_passwords(stream, aliases):
    """`alias<TAB>password` lines from stdin, for exactly the plan's aliases. A refusal never names or echoes a line."""
    out = {}
    for line in stream.read(1 << 16).splitlines():
        if not line.strip():
            continue
        alias, sep, password = line.partition('\t')
        if not sep or alias not in aliases or not password or alias in out:
            raise Refused('stdin: one alias<TAB>password line per account of the plan')
        out[alias] = password
    if set(out) != set(aliases):
        raise Refused('stdin: a password for every account of the plan (and no other)')
    return out


def persona_of(db, tenant, principal):
    """(consumer persona or None, erasing) of the principal's Consumer, read only."""
    db.execute("select set_config('eios.tenant_id',%s,false)", (tenant,))
    row = db.execute("""select p.persona,exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=o.tenant_id and e.world=o.world
            and e.consumer_id=o.consumer_id and e.state<>'pending_confirmation')
        from control.nexloop_consumer_owners o left join control.nexloop_consumer_personas p on p.tenant_id=o.tenant_id and p.world=o.world and p.consumer_id=o.consumer_id
        where o.tenant_id=%s and o.world='real' and o.principal_id=%s""", (tenant, principal)).fetchall()
    if not row:
        return 'no_consumer', False  # never chatted, or erased (the erasure removes the owner row): nothing is sent
    return row[0][0], row[0][1]


class Driver:
    def __init__(self, config, plan, passwords, *, client_factory=None, connect=None, sleep=time.sleep, clock=time.monotonic, out=sys.stdout):
        self.config, self.plan, self.passwords = config, plan, passwords
        self.client_factory = client_factory or (lambda: ConsumerClient(config['origin'], ca_file=config.get('ca_file'), sleep=sleep, clock=clock))
        self.connect = connect or (lambda: C.connect_read_only(config['reader_dsn_file'], who='reader'))
        self.sleep, self.clock, self.out = sleep, clock, out
        self.skipped = set()

    def emit(self, doc):
        print(json.dumps(doc, ensure_ascii=False, sort_keys=True), file=self.out, flush=True)

    def login(self, alias):
        client = self.client_factory()
        for attempt in range(LOGIN_TRIES):
            try:
                client.login(self.config['accounts'][alias], self.passwords[alias])
                return client
            except ConsumerChannelError as error:
                if error.status != 429 or attempt == LOGIN_TRIES - 1:
                    raise
                self.sleep(min(MAX_BACKOFF, error.retry_after or 2 ** attempt))
        raise ConsumerChannelError('login_throttled', 429)

    @staticmethod
    def logout(client):
        try:
            client.logout()
        except ConsumerChannelError:
            pass

    def preflight(self):
        """Every account logs in once and its Consumer's persona is read; a real (or unknown) one refuses the whole run."""
        aliases = list(dict.fromkeys(t['account'] for t in self.plan['turns']))
        report = {}
        with self.connect() as db:
            for alias in aliases:
                try:
                    client = self.login(alias)
                except ConsumerChannelError as error:
                    raise Refused(f'preflight: {alias} login {error.code}') from None
                session = client.session
                self.logout(client)
                persona, erasing = persona_of(db, session.tenant_id, session.principal_id)
                if erasing or persona == 'no_consumer':
                    self.skipped.add(alias)  # an erased (or never created) Consumer: skipped, never created here
                    report[alias] = 'conversation_unavailable'
                elif persona in SYNTHETIC:
                    report[alias] = persona
                else:
                    raise Refused(f'preflight: {alias} is not a simulated_agent / synthetic Consumer ({persona or "no persona"}); nothing sent')
        self.emit({'preflight': report})
        return report

    def turn(self, number, t):
        alias, started = t['account'], self.clock()
        doc = {'turn': number, 'account': alias, 'replied': None}
        if alias in self.skipped:
            return {**doc, 'status': 'conversation_unavailable', 'http': None, 'elapsed_ms': 0}
        client = None
        try:
            client = self.login(alias)
            listed = client.list_conversations(limit=1).get('items') or []
            if listed:
                conversation = listed[0]['id']
            else:
                try:
                    conversation = client.create_conversation(f"{self.plan['plan_id']}-conversation-{alias}")['conversation']['id']
                except ConsumerChannelError as error:
                    if error.status == 403:
                        self.skipped.add(alias)
                        return {**doc, 'status': 'conversation_unavailable', 'http': 403}
                    raise
            sent = client.send(t['body'], conversation_id=conversation, idempotency_key=f"{self.plan['plan_id']}-{number}-{alias}")
            doc.update(status='sent' if sent.get('created', True) else 'replayed', http=200)
            if t.get('wait_reply_seconds'):
                after = int((sent.get('message') or {}).get('sequence') or 0)
                got = client.wait_reply(conversation, after_sequence=after, timeout=t['wait_reply_seconds'])
                doc['replied'] = bool(got.get('replied'))
                if not got.get('replied'):
                    doc['status'] = 'timeout' if doc['status'] == 'sent' else 'replayed_timeout'
            return doc
        except ConsumerChannelError as error:
            if error.status == 403:
                self.skipped.add(alias)
                return {**doc, 'status': 'conversation_unavailable', 'http': 403}
            return {**doc, 'status': error.code, 'http': error.status}
        finally:
            if client is not None:
                self.logout(client)
            doc['elapsed_ms'] = int((self.clock() - started) * 1000)

    def run(self):
        self.preflight()
        results = []
        for number, t in enumerate(self.plan['turns'], 1):
            doc = self.turn(number, t)
            doc.setdefault('elapsed_ms', 0)
            self.emit(doc)
            results.append(doc)
        ok = [r for r in results if r['status'] in ('sent', 'replayed')]
        self.emit({'summary': {'turns': len(results), 'sent_or_replayed': len(ok), 'replied': sum(1 for r in results if r['replied']),
                               'not_sent': {s: sum(1 for r in results if r['status'] == s) for s in sorted({r['status'] for r in results} - {'sent', 'replayed'})}}})
        return 0 if len(ok) == len(results) else 1


def main(argv=None, stdin=sys.stdin, out=sys.stdout, **driver):
    p = argparse.ArgumentParser(description='Synthetic customers speak in the WebChat (passwords on stdin)')
    p.add_argument('--config', required=True)
    p.add_argument('--plan', required=True)
    a = p.parse_args(argv)
    try:
        config, plan = load(a.config, a.plan)
        passwords = read_passwords(stdin, {t['account'] for t in plan['turns']})
        return Driver(config, plan, passwords, out=out, **driver).run()
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    except Exception as error:  # noqa: BLE001 - never a message, body or password: the type only
        print(json.dumps({'refused': 'unexpected ' + type(error).__name__}), file=out)
        return 2


if __name__ == '__main__':
    sys.exit(main())
