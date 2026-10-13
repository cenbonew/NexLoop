"""NX-031 slice 1: the inbound load driver (design §3.1). One asyncio process, API only (loopback HTTPS, never the database).

Customers log into the WebChat realm with their own synthetic accounts (warm-up, not measured), then messages arrive as a
Poisson process at the cell's rate; the customer is drawn from a Zipf distribution and the kind from the matrix mix, all from a
fixed seed. Records only what the results contract allows: ACK latency and status (acks.jsonl). The workbench side runs the
owner's overview read and an operator's takeover → staff reply → hand-back cycle. After the window it probes cross-customer
access (design §4.4 item 5). No message text is written anywhere by the driver.
"""
import asyncio
import json
import random
import socket
import time

import httpx

TEXT = {
    'ask': ('请问续费价格是多少？', '订单什么时候发货？', '能帮我查一下进度吗？', '可以开发票吗？', '使用说明在哪里看？'),
    'commitment_ask': ('请在两天内给我回个电话。', '麻烦明天之前把发票发给我。', '能不能下周一前寄出来？'),
    'refusal': ('以后不要再给我发消息了。', '请不要再联系我。'),
    'followup': ('好的，谢谢。', '明白了。', '收到。'),
}


def source_address(index):
    """A distinct loopback source address per customer (127.0.0.0/8; as customers arriving from different addresses), or None
    when this host cannot bind one (macOS binds only 127.0.0.1 without an alias): the login rate limit then applies to all
    customers as one client and the warm-up waits out its Retry-After."""
    address = f'127.{index // (254 * 254) % 254}.{index // 254 % 254}.{index % 254 + 1}'
    try:
        with socket.socket() as probe:
            probe.bind((address, 0))
        return address
    except OSError:
        return None


class Customer:
    def __init__(self, index, account, origin, ca):
        self.index, self.account, self.origin = index, account, origin
        self.source = source_address(index + 2)
        transport = httpx.AsyncHTTPTransport(verify=ca, local_address=self.source, limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
        self.client = httpx.AsyncClient(base_url=origin, verify=ca, timeout=30, trust_env=False, transport=transport)
        self.csrf, self.conversation = None, None

    async def login(self, waited=0.0, attempts=None):
        """Warm-up, not measured as load: under the tenant's login rate policy (never relaxed for the load), a 429 waits its
        Retry-After. Each login request's latency and status are appended to `attempts` (numbers only; the login peak retest,
        dispatcher 2026-10-13)."""
        while True:
            started = time.perf_counter()
            try:
                response = await self.client.post('/api/v1/auth/login', headers={'Origin': self.origin},
                                                  json={'username': self.account['username'], 'password': self.account['password']})
            except httpx.HTTPError:
                if attempts is not None:
                    attempts.append((round((time.perf_counter() - started) * 1000, 1), 0))
                raise
            if attempts is not None:
                attempts.append((round((time.perf_counter() - started) * 1000, 1), response.status_code))
            if response.status_code != 429 or waited > 3600:
                break
            pause = float(response.headers.get('Retry-After', '5'))
            waited += pause
            await asyncio.sleep(pause)
        response.raise_for_status()
        self.csrf = response.json()['csrf_token']
        listed = (await self.client.get('/api/v1/conversations')).json()['items']
        self.conversation = listed[0]['id'] if listed else (await self.client.post(
            '/api/v1/conversations', headers=self.headers(f'load-new-{self.index:05d}'), json={})).json()['id']

    def headers(self, key):
        return {'Origin': self.origin, 'X-CSRF-Token': self.csrf, 'Idempotency-Key': key}

    async def send(self, body, key):
        started = time.perf_counter()
        try:
            response = await self.client.post(f'/api/v1/conversations/{self.conversation}/messages', headers=self.headers(key), json={'body': body})
            status = response.status_code
        except httpx.HTTPError:
            status = 0
        return round((time.perf_counter() - started) * 1000, 1), status


class Driver:
    def __init__(self, *, origins, ca, accounts, restricted, workbench, matrix, seed):
        self.rng = random.Random(f'nexloop-load-driver:{seed}')
        self.customers = [Customer(i, a, origins[i % len(origins)], ca) for i, a in enumerate(accounts)]
        self.restricted = [c for c in self.customers if c.account['username'] in restricted]
        self.origins, self.ca, self.workbench, self.matrix = origins, ca, workbench, matrix
        s = matrix['load']['zipf_s']
        weights = [1 / (rank ** s) for rank in range(1, len(self.customers) + 1)]
        order = list(range(len(self.customers)))
        self.rng.shuffle(order)
        self.population, self.weights = [self.customers[i] for i in order], weights
        self.mix = list(matrix['load']['message_mix'].items())
        self.sent, self.keys = 0, []

    async def login_all(self, parallel=8):
        """Every customer logs in, `parallel` at a time (the login peak). Per request latency/status go to self.login_attempts."""
        gate = asyncio.Semaphore(parallel)
        failed = 0
        self.login_attempts = []

        async def one(customer):
            nonlocal failed
            async with gate:
                try:
                    await customer.login(attempts=self.login_attempts)
                except (httpx.HTTPError, KeyError, ValueError):
                    failed += 1
        await asyncio.gather(*(one(c) for c in self.customers))
        return failed

    def _pick(self):
        kind = self.rng.choices([k for k, _ in self.mix], [w for _, w in self.mix])[0]
        if kind == 'to_restricted' and self.restricted:
            return self.rng.choice(self.restricted), 'ask'
        return self.rng.choices(self.population, self.weights)[0], ('ask' if kind == 'to_restricted' else kind)

    async def _message(self, record):
        customer, kind = self._pick()
        if customer.csrf is None:
            return
        if kind == 'replay' and self.keys:
            target, key, body = self.rng.choice(self.keys)
            record(await target.send(body, key))
            return
        bursts = 3 if kind == 'burst3' else 1
        for _ in range(bursts):
            body = self.rng.choice(TEXT['followup' if kind in ('burst3', 'followup') else kind if kind in TEXT else 'ask'])
            self.sent += 1
            key = f'load-msg-{self.sent:08d}-{self.rng.getrandbits(32):08x}'
            self.keys.append((customer, key, body))
            record(await customer.send(body, key))

    async def run_phase(self, seconds, rate, record):
        """Poisson arrivals for `seconds`; each arrival is one task (a slow ACK never delays later arrivals)."""
        tasks, end = set(), time.monotonic() + seconds
        while time.monotonic() < end:
            await asyncio.sleep(self.rng.expovariate(rate))
            task = asyncio.create_task(self._message(record))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.wait(tasks, timeout=60)

    async def workbench_activity(self, seconds, events):
        """Owner overview reads and one operator takeover → staff reply → hand-back cycle (governed write entry)."""
        if not self.workbench:
            return
        origin = self.origins[0]
        async with httpx.AsyncClient(base_url=origin, verify=self.ca, timeout=30, trust_env=False) as owner, \
                   httpx.AsyncClient(base_url=origin, verify=self.ca, timeout=30, trust_env=False) as operator:
            for client, who in ((owner, 'owner'), (operator, 'operator-1')):
                response = await client.post('/api/v1/workbench/auth/login', headers={'Origin': origin},
                                             json={k: self.workbench[who][k] for k in ('username', 'password')})
                events.append({'workbench_login': who, 'status': response.status_code})
            end, next_cycle = time.monotonic() + seconds, time.monotonic()
            overview_every = self.matrix['load']['workbench']['overview_every_seconds']
            cycle_every = self.matrix['load']['workbench']['takeover_cycle_every_seconds']
            while time.monotonic() < end:
                events.append({'overview': (await owner.get('/api/v1/workbench/overview')).status_code})
                if time.monotonic() >= next_cycle:
                    next_cycle += cycle_every
                    events.append({'takeover_cycle': await self._takeover_cycle(operator, origin)})
                await asyncio.sleep(min(overview_every, max(0.1, end - time.monotonic())))

    async def _takeover_cycle(self, client, origin):
        customer = self.rng.choice([c for c in self.customers if c.conversation])
        async def act(operation, body):
            csrf = (await client.post('/api/v1/workbench/auth/csrf', headers={'Origin': origin})).json()['csrf_token']
            response = await client.post('/api/v1/workbench/actions/' + operation, json=body,
                headers={'Origin': origin, 'X-CSRF-Token': csrf, 'Idempotency-Key': f'load-wb-{self.rng.getrandbits(64):016x}'})
            return response.status_code, (response.json() if response.headers.get('content-type', '').startswith('application/json') else {})
        status, started = await act('take_over_conversation', {'scope_kind': 'conversation', 'scope_ref': customer.conversation,
                                                               'duration_seconds': 1800, 'reason': '压测：人工接管'})
        if status != 200:
            return {'takeover': status}
        view = (await client.get('/api/v1/workbench/conversations/' + customer.conversation)).json()
        inbound = [m['id'] for m in view.get('messages', []) if m.get('direction') == 'inbound']
        reply = (await act('send_staff_reply', {'conversation_id': customer.conversation, 'reply_to': inbound[-1], 'text': '您好，人工客服正在处理。'}))[0] if inbound else None
        handed = (await act('hand_back_conversation', {'takeover_id': started.get('takeover_id', ''), 'reason': '压测：交还'}))[0]
        return {'takeover': status, 'staff_reply': reply, 'handback': handed}

    async def cross_access_probe(self, pairs):
        """Customer A reads customer B's conversation: must be refused (403/404)."""
        ready = [c for c in self.customers if c.conversation and c.csrf]
        checked = violations = 0
        for _ in range(min(pairs, max(0, len(ready) - 1))):
            a, b = self.rng.sample(ready, 2)
            response = await a.client.get(f'/api/v1/conversations/{b.conversation}/messages')
            checked += 1
            violations += response.status_code == 200
        return {'checked': checked, 'violations': violations}

    async def close(self):
        await asyncio.gather(*(c.client.aclose() for c in self.customers))
