#!/usr/bin/env python3
"""S6 closure: the stage acceptance runs of NX-038 / NX-039 (L5 closure checklist §2, "stage" column), one check per AT. The shared
configuration, result format, blocked semantics and redaction are scripts/acceptance/s6_common.py's (with L2's NX-040 part,
scripts/acceptance/s6_stage_schema.py).

Every step goes through the services' own entry points, as the services connect - the read-only reader nexloop_evo_reader (E2/E3
reads), the writer nexloop_evo_writer (diagnosis, candidates, bundles) and the evaluator nexloop_evo_evaluator (open, conclude, L1)
- each with its own service credential read from a private file. A separate inspector connection (on stage the bootstrap DSN from
the private configuration, set read only on connect) only probes (protected-table digests, candidate and holdout states,
credentials and role entries). Data: the synthetic Consumers' Runs in the configured window and the world test for L1 (the
registered test-only evaluation credential). Nothing here holds a model, channel or effect credential, publishes a definition or
writes a formal object; L1 runs are the D16 synthetic stand-in. Registering principals and credentials is not done here.

    uv run --frozen python scripts/acceptance/s6_stage_evolution.py <check> --config F [--dry-run]

    checks: at046 r5 at048 r4 at023 r1 r3 r7-prepare r7-verify candidate-template all
    (r7: prepare, then the owner erases the synthetic Consumer with scripts/deploy/erase_synthetic_consumer.py, then verify;
     candidate-template writes inputs.candidate_template, the open candidate body L2's NX-040 checks start from)

Configuration (s6_common, nexloop-s6-stage/1; deploy/examples/s6-stage.example.json): roles.writer / roles.evaluator
{dsn_file, token_file}, roles.reader {dsn_file, token_file}, roles.inspector {dsn_file}, roles.evaluation {token_file} (the
test-only evaluation credential), roles.registrar {dsn_file} (optional, R5's registration negative only); inputs.evolution {window
{since, until}, personas, label, parent_revision, evaluator_version, model_profile, created_at, l0_inputs, protected_refs,
erasure_consumer_id, and optionally extraction_file}; inputs.candidate_template (a path). extraction_file (I34 b/c, an absolute path):
L3's derived {access_id, extraction} rows for access records older than the extraction column; a recorded value always wins. Private state (R7) lives under results_dir/state.

--dry-run calls no writing entry: it checks the configuration and the role of every connection, reads the inputs, plans each
candidate in memory and probes the holdout uses. Output and exit: s6_common's (passed null when blocked or a dry run).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/eios-core/src'))

import psycopg  # noqa: E402

from nexloop_eios.evolution import evaluation as E  # noqa: E402
from nexloop_eios.evolution.attribution import attribute, draft_candidates, load_extraction, read_missed, with_extraction  # noqa: E402
from nexloop_eios.evolution.l1 import run_l1  # noqa: E402
from nexloop_eios.evolution_store import CandidateStore, EvaluatorStore, diagnosis_id  # noqa: E402
from nexloop_eios.evolution_traces import DatabaseTraceSource  # noqa: E402
from nexloop_eios.private_configuration import read_private_text  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s6_common as C  # noqa: E402

ROLES = {'reader': 'nexloop_evo_reader', 'writer': 'nexloop_evo_writer', 'evaluator': 'nexloop_evo_evaluator'}
# The protected tables of AT-023 (formal definitions and objects, Claims, candidate definitions, budget limits, authority facts and
# grants): the same set as the E4 test's PROTECTED.
PROTECTED = ('ontology.object_type_versions', 'ontology.objects', 'ontology.nexloop_claims', 'ontology.nexloop_candidate_definitions',
             'control.nexloop_budget_limits', 'authz.nexloop_authority_facts', 'authz.nexloop_service_grants')
# One holdout (validation set) per AT that opens evaluations: at most 5 candidates are decided per holdout (D39-3), so the ATs never
# share one (R4 spends its own on purpose).
HOLDOUT_ORDER = ('at023', 'r1', 'r3', 'r4', 'at048', 'r5', 'r7', 'template')
ACCEPTANCE = {'at046': ['AT-046'], 'r5': ['S6-R5'], 'at048': ['AT-048'], 'r4': ['S6-R4'], 'at023': ['AT-023'], 'r1': ['S6-R1'], 'r3': ['S6-R3'],
              'r7-prepare': ['S6-R7'], 'r7-verify': ['S6-R7'], 'candidate-template': ['AT-024', 'S6-R8', 'S6-R10']}
HEX = re.compile(r'[0-9a-f]{64}')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
Refused = C.Refused


class Precondition(C.Blocked):
    """The stage data or registration does not allow the AT (the check is blocked: passed null, never a pass)."""


# --- configuration ----------------------------------------------------------------------------------------------------------------

def load_config(path):
    """s6_common's configuration plus this part's roles and inputs.evolution; returns (config, the evolution settings)."""
    config = C.load_config(path)
    roles, inputs = config.get('roles'), config.get('inputs')
    if type(roles) is not dict or type(inputs) is not dict:
        raise Refused('configuration: roles and inputs required')
    def role(name, keys, optional=False):
        v = roles.get(name)
        if v is None and optional:
            return None
        if type(v) is not dict or set(v) != set(keys) or any(type(v[k]) is not str or not os.path.isabs(v[k]) for k in keys):
            raise Refused(f'roles.{name}: {{{", ".join(keys)}}} (absolute paths of private files)')
        return v
    rw = {n: role(n, ('dsn_file', 'token_file')) for n in ('reader', 'writer', 'evaluator')}
    inspector, evaluation = role('inspector', ('dsn_file',)), role('evaluation', ('token_file',))
    registrar = role('registrar', ('dsn_file',), optional=True)
    e = inputs.get('evolution')
    keys = {'window', 'personas', 'label', 'parent_revision', 'evaluator_version', 'model_profile', 'created_at', 'l0_inputs', 'protected_refs',
            'erasure_consumer_id'}
    if type(e) is not dict or set(e) - {'extraction_file'} != keys:
        raise Refused(f'inputs.evolution keys: {sorted(keys)} (and optionally extraction_file)')
    extraction_file = e.get('extraction_file')
    if extraction_file is not None and (type(extraction_file) is not str or not os.path.isabs(extraction_file)):
        raise Refused('inputs.evolution.extraction_file: an absolute path (I34 c derivation)')
    try:
        since, until = datetime.fromisoformat(e['window']['since']), datetime.fromisoformat(e['window']['until'])
        created = datetime.fromisoformat(e['created_at'].replace('Z', '+00:00'))
    except (TypeError, KeyError, ValueError, AttributeError):
        raise Refused('inputs.evolution: window {since, until} and created_at, ISO times with zone') from None
    if since.tzinfo is None or until.tzinfo is None or created.tzinfo is None or not since < until:
        raise Refused('inputs.evolution.window: since < until, with zone')
    if type(e['personas']) is not list or not e['personas'] or not set(e['personas']) <= {'simulated_agent', 'synthetic', 'real'}:
        raise Refused('inputs.evolution.personas: simulated_agent / synthetic (real only for a deliberate run)')
    for k in ('label', 'parent_revision', 'evaluator_version', 'model_profile'):
        if type(e[k]) is not str or not LABEL.fullmatch(e[k]):
            raise Refused(f'inputs.evolution.{k}: [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}')
    if type(e['l0_inputs']) is not dict or set(e['l0_inputs']) - {'latency_ms', 'alias_counts', 'alias_index', 'noted_refs'}:
        raise Refused('inputs.evolution.l0_inputs: the measured inputs of L0 (latency_ms, alias_counts, alias_index, noted_refs)')
    if type(e['protected_refs']) is not list or any(type(r) is not str or len(r) > 512 for r in e['protected_refs']):
        raise Refused('inputs.evolution.protected_refs: definition refs')
    if not (e['erasure_consumer_id'] is None or (type(e['erasure_consumer_id']) is str and HEX.fullmatch(e['erasure_consumer_id']))):
        raise Refused('inputs.evolution.erasure_consumer_id: 64 hex | null')
    template = inputs.get('candidate_template')
    if template is not None and (type(template) is not str or not os.path.isabs(template)):
        raise Refused('inputs.candidate_template: an absolute path')
    connections = {n: v['dsn_file'] for n, v in rw.items()} | {'inspector': inspector['dsn_file']} | ({'registrar': registrar['dsn_file']} if registrar else {})
    credentials = {n: v['token_file'] for n, v in rw.items()} | {'evaluation': evaluation['token_file']}
    ev = {'connections': connections, 'credentials': credentials, 'window': {'since': since, 'until': until}, 'personas': e['personas'],
          'label': e['label'], 'parent_revision': e['parent_revision'], 'evaluator_version': e['evaluator_version'], 'model_profile': e['model_profile'],
          'created_at': e['created_at'], 'l0_inputs': e['l0_inputs'], 'protected_refs': e['protected_refs'],
          'erasure': {'consumer_id': e['erasure_consumer_id']}, 'state_dir': str(Path(config['results_dir']) / 'state'), 'tenant_id': config['tenant_id'],
          'candidate_template': template, 'extraction_file': extraction_file}
    return config, ev


def _digest_of(path):
    return hashlib.sha256(read_private_text(path, maximum=16384).strip().encode()).hexdigest()


def connector(path, *, read_only=False):
    def connect():
        conn = psycopg.connect(read_private_text(path, maximum=4096), autocommit=True, connect_timeout=10)
        if read_only:
            conn.execute('set default_transaction_read_only=on')
        return conn
    return connect


def _uuid(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'nexloop:s6-stage:' + ':'.join(parts)))


def refusal(error):
    """The database's own refusal text (the ports may hide it in the chain)."""
    while error is not None:
        if isinstance(error, psycopg.Error):
            return error.diag.message_primary
        error = error.__cause__ or error.__context__
    return None


# --- the result: s6_common's, with this part's step helpers --------------------------------------------------------------------------

class Result:
    def __init__(self, check, config, dry_run):
        self.res = C.Result(check, ACCEPTANCE[check], config, dry_run=dry_run)
        self.dry_run = dry_run
        self.notes, self.digests = [], {}

    def step(self, name, ok=True, **detail):
        """A procedural step (planned, probed, done). A failed precondition in a dry run is a block (never a pass)."""
        if not ok and self.dry_run:
            self.res.block(name, json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str))
            return
        self.res.step(name, 'ok', 'ok' if ok else 'not ok', ok, **detail)

    def check(self, name, expected, actual):
        return self.res.step(name, expected, actual, expected == actual)

    def block(self, name, reason):
        self.res.block(name, reason)

    def write(self, out):
        for note in self.notes:
            self.res.step('note', None, None, True, note=note)
        if self.digests:
            self.res.steps.append({'name': 'protected table digests', 'expected': None, 'observed': None, 'passed': True, 'evidence': self.digests})
        return self.res.write(out)


# --- the stage ----------------------------------------------------------------------------------------------------------------------

class Stage:
    def __init__(self, cfg):
        self.cfg = cfg
        c, k = cfg['connections'], cfg['credentials']
        self.connect = {role: connector(c[role]) for role in ROLES}
        self.inspect = connector(c['inspector'], read_only=True)
        self.registrar = connector(c['registrar']) if c.get('registrar') else None
        self.writer = CandidateStore(self.connect['writer'], k['writer'])
        self.evaluator = EvaluatorStore(self.connect['evaluator'], k['evaluator'])
        self.writer_as_evaluator = EvaluatorStore(self.connect['evaluator'], k['writer'])
        self.reader = DatabaseTraceSource(self.connect['reader'], k['reader'])
        self._inputs = None

    # roles and inputs (read only) ------------------------------------------------------------------------------------------------
    def roles(self, result):
        for role, expected in ROLES.items():
            with self.connect[role]() as db:
                got = db.execute('select current_user').fetchone()[0]
            result.step(f'connection {role} is {expected}', got == expected, role=got)
        with self.inspect() as db:
            ro = db.execute('show default_transaction_read_only').fetchone()[0]
        result.step('inspector is read only', ro == 'on')

    def inputs(self):
        """The reader's samples and access records of the window, the diagnosis and its drafts, the clusters and holdout units."""
        if self._inputs is None:
            cfg = self.cfg
            read = self.reader.read(cfg['window']['since'], cfg['window']['until'], personas=tuple(cfg['personas']))
            runs = {s['run_id'] for s in read.samples}
            with self.connect['reader']() as db:
                rows = db.execute('select authz.nexloop_semantic_access_read(%s,%s,%s,%s)',
                                  (_digest_of(cfg['credentials']['reader']), 'real', cfg['window']['since'], 1000)).fetchone()[0]
            accesses = [a for a in rows if a.get('run_id') in runs]
            if cfg.get('extraction_file'):  # I34 (c): records older than the extraction column get L3's derived value
                with open(cfg['extraction_file'], encoding='utf-8') as f:
                    accesses = with_extraction(accesses, load_extraction(f.read()))
            diagnosis = attribute(read.samples, accesses)
            tenant = self._tenant()  # the drafts carry the writer credential's tenant (SQL checks it against the credential)
            drafts, skipped = draft_candidates(diagnosis, read.samples, tenant_id=tenant, parent_revision=cfg['parent_revision'],
                                               evaluator_version=cfg['evaluator_version'], model_profile=cfg['model_profile'], created_at=cfg['created_at'])
            if not drafts:
                raise Precondition(f'no semantic_content draft from the window (skipped {skipped}); the stage needs failed Runs of synthetic Consumers with retrieval misses')
            base = drafts[0]
            clusters = dict(base.case_groups)
            terms = {}
            for a in accesses:
                # I34 (b), open: excluding covered reads here merges every group into one unit (the group's runs share its missed
                # term by construction), so the near-duplicate input stays every read until the dispatcher decides (see the report).
                if a.get('kind') == 'access' and a['run_id'] in clusters:
                    terms.setdefault(a['run_id'], set()).update(a.get('term_hashes') or [])
            # Near duplicates across clusters (open check 3): the clusters they join are one unit, so no AT but AT-048 splits them.
            pairs = sorted((a, b) for a, b in itertools.combinations(sorted(terms), 2) if terms[a] == terms[b] and clusters[a] != clusters[b])
            parent = {c: c for c in set(clusters.values())}

            def find(x):
                while parent[x] != x:
                    x = parent[x]
                return x
            for a, b in pairs:
                parent[find(clusters[a])] = find(clusters[b])
            units = {}
            for c in sorted(set(clusters.values())):
                units.setdefault(find(c), []).append(c)
            self._inputs = dict(samples=read.samples, accesses=accesses, diagnosis=diagnosis, base=base, clusters=clusters, terms=terms, pairs=pairs,
                                units=[sorted(v) for _, v in sorted(units.items())], tenant=tenant, excluded=read.excluded)
        return self._inputs

    def _tenant(self):
        return self.cfg['tenant_id']

    def holdout_units(self, at):
        """The validation clusters of an AT's holdout: the AT's own subset of the units (never all of them: design is not empty)."""
        units = self.inputs()['units']
        subsets = [s for n in range(1, len(units)) for s in itertools.combinations(range(len(units)), n)]
        i = HOLDOUT_ORDER.index(at)
        if i >= len(subsets):
            raise Precondition(f'{len(units)} cluster units give {len(subsets)} holdouts; {len(HOLDOUT_ORDER)} are needed (synthetic Consumers from at least 4 units)')
        return sorted(c for u in subsets[i] for c in units[u])

    def digests(self):
        out = {}
        with self.inspect() as db:
            for t in PROTECTED:
                if db.execute('select to_regclass(%s)', (t,)).fetchone()[0]:
                    out[t] = db.execute(f"select md5(coalesce(string_agg(x::text,'|' order by x::text),'')) from {t} x").fetchone()[0]
        return out

    def holdout_uses(self, validation):
        with self.inspect() as db:
            return db.execute('select count(*) from runtime.nexloop_evo_holdout_uses where holdout_digest=runtime.nexloop_evo_holdout_digest(%s)',
                              (validation,)).fetchone()[0]

    # candidates ------------------------------------------------------------------------------------------------------------------
    def plan(self, label, validation_clusters, *, groups=None, design=None, validation=None, patch=None, dimension=None):
        """A candidate of the base draft with its own id and split (pure)."""
        i = self.inputs()
        d = i['base']
        runs = sorted(d.candidate['design_case_ids'] + d.candidate['validation_case_ids'])
        clusters = groups or i['clusters']
        if validation is None:
            validation = [r for r in runs if i['clusters'][r] in validation_clusters]
            design = [r for r in runs if r not in validation]
        cand = copy.deepcopy(d.candidate)
        cid = _uuid(self.cfg['label'], label)
        cand.update(candidate_id=cid, candidate_revision=f"{self.cfg['parent_revision']}+s6.{cid[:8]}", design_case_ids=sorted(design),
                    validation_case_ids=sorted(validation))
        if dimension:
            cand['dimension'] = dimension
        p = copy.deepcopy(d.patch)
        p.update(patch or {})
        sources = sorted({s['consumer_id'] for s in i['samples'] if s['run_id'] in runs and s.get('consumer_id')})
        cases = sorted(design) + sorted(validation)  # the groups name exactly the cases (SQL checks it)
        return type(d)(cand, d.category, p, {r: clusters[r] for r in cases}, d.regroups, sources)

    def seed(self, label):
        return hashlib.sha256(f"{self.cfg['label']}:{label}".encode()).hexdigest()[:32]

    def propose(self, draft):
        diag = self.writer.open_diagnosis(self.inputs()['diagnosis'], self.inputs()['samples'], parent_revision=self.cfg['parent_revision'],
                                          evaluator_version=self.cfg['evaluator_version'], model_profile=self.cfg['model_profile'])
        return diag, self.writer.propose(diagnosis_id(self.inputs()['diagnosis'].snapshot_digest), draft)

    def freeze_open(self, draft, label, levels=('L0',)):
        self.propose(draft)
        frozen = self.writer.freeze_bundle(draft.candidate['candidate_id'], levels=levels, seed=self.seed(label))
        opened = self.evaluator.open(draft.candidate['candidate_id'], frozen['bundle_digest'])
        return frozen, opened

    def case_accesses(self, bundle):
        cases = set(bundle['design_case_ids']) | set(bundle['validation_case_ids'])
        return [a for a in self.inputs()['accesses'] if a.get('run_id') in cases]

    def l0(self, frozen, draft, inputs=None):
        records = self.case_accesses(frozen['bundle'])
        return E.evaluate_l0(frozen['bundle'], frozen['bundle_digest'], draft.patch, [a for a in records if a['kind'] == 'access'],
                             uses=[a for a in records if a['kind'] == 'use'], inputs=self.cfg['l0_inputs'] if inputs is None else inputs,
                             created_at=self.cfg['created_at'])

    def candidate_state(self, cid):
        with self.inspect() as db:
            return db.execute('select status,end_reason from runtime.nexloop_evo_candidates where candidate_id=%s', (cid,)).fetchone()


def synthetic_runner(case, variant, i):
    """D16: the L1 stand-in - counts and flags only (no Run, no model, no effect); the candidate succeeds, the parent once in three."""
    return {'success': variant == 'candidate' or i == 1, 'tool_param_errors': 0, 'model_calls': 0, 'cost': '0.01', 'violations': {}}


# --- the ATs -------------------------------------------------------------------------------------------------------------------------

def at046(st, r):
    """AT-046 regression (S6 part): the evaluation credential is test only and registered; it never authenticates in real; the writer
    and the evaluator reach their own evolution entries only (no effect, message or dispatch entry); real dispatch is closed for test."""
    k = st.cfg['credentials']
    ev, wr, ed = _digest_of(k['evaluation']), _digest_of(k['writer']), _digest_of(k['evaluator'])
    with st.inspect() as db:
        worlds = db.execute('select worlds from authz.nexloop_service_credentials where token_digest=%s', (ev,)).fetchone()
        registered = db.execute('select count(*) from authz.nexloop_evo_evaluation_credentials where token_digest=%s', (ev,)).fetchone()[0]
        service = {d: (db.execute('select worlds from authz.nexloop_service_credentials where token_digest=%s', (d,)).fetchone() or [None])[0] for d in (wr, ed)}
        entries = {}
        for role in ('nexloop_evo_writer', 'nexloop_evo_evaluator'):
            entries[role] = sorted({x[0].split('(')[0] for x in db.execute("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                where n.nspname in ('authz','control','runtime','ontology') and p.prosecdef and has_function_privilege(%s,p.oid,'execute')""", (role,))})
        try:
            with db.transaction():
                db.execute("select authz.nexloop_service_identity_snapshot(%s,'real')", (ev,))
            real_auth = 'authenticated'
        except psycopg.Error as error:
            real_auth = 'refused' if error.sqlstate == '42501' else f'error:{error.sqlstate}'
        layers = []
        # Safe only because the world here is test (never real): closed_layer is a read of the switch layers, and forcing the deployment
        # switch on in this transaction cannot open a world that is not real (L6 nx043-readiness review, optional). Do not point this at
        # world 'real': there the forced switch would be the only layer left between this probe and a real dispatch decision.
        for setting in ('', 'true'):  # the deployment switch off, then forced on: world test stays closed by the world itself
            with db.transaction():
                db.execute("select set_config('nexloop.real_dispatch_enabled',%s,true)", (setting,))
                layers.append(db.execute("select control.nexloop_real_dispatch_closed_layer(%s,'test','eios:action:nexloop.message.staff_send:1')",
                                         (st._tenant(),)).fetchone()[0])
    r.step('probe credentials, role entries and dispatch', True)
    if r.dry_run:
        return
    r.check('evaluation credential worlds', [['test']], [worlds[0]] if worlds else [None])
    r.check('evaluation credential registered', 1, registered)
    r.check('evaluation credential never authenticates in real', 'refused', real_auth)
    r.check('writer and evaluator credentials are real-world service credentials', {wr: ['real'], ed: ['real']}, service)
    # Their own entries: NX-038/039 (authz.nexloop_evo_*) and, for the writer, NX-040's schema proposal entries; the public erasure helper.
    allowed = {'nexloop_evo_writer': ('authz.nexloop_evo_', 'authz.nexloop_schema_proposal_'), 'nexloop_evo_evaluator': ('authz.nexloop_evo_',)}
    stray = {role: [e for e in names if not e.startswith(allowed[role]) and e != 'runtime.nexloop_erasure_permits'] for role, names in entries.items()}
    r.check('writer and evaluator reach their evolution entries only (no effect entry)', {'nexloop_evo_writer': [], 'nexloop_evo_evaluator': []}, stray)
    r.check('real dispatch closed for world test (switch off, then forced on)', True, layers[0] in ('deployment', 'world') and layers[1] == 'world')


def r5(st, r):
    """S6-R5: a writer credential cannot open an evaluation (evaluator port and the writer role); a non-test evaluation credential is
    refused (L1 open; and its registration, when a registrar connection is configured)."""
    draft = st.plan('r5', st.holdout_units('r5'))
    r.step('plan', True, candidate=draft.candidate['candidate_id'], validation=len(draft.candidate['validation_case_ids']),
           holdout_uses=st.holdout_uses(draft.candidate['validation_case_ids']))
    if r.dry_run:
        return
    st.propose(draft)
    frozen = st.writer.freeze_bundle(draft.candidate['candidate_id'], levels=('L0', 'L1'), seed=st.seed('r5'))
    c, bd = draft.candidate['candidate_id'], frozen['bundle_digest']
    try:
        st.writer_as_evaluator.open(c, bd)
        got = 'opened'
    except Exception as error:  # noqa: BLE001 - the refusal is the result
        got = refusal(error) or type(error).__name__
    r.check('writer credential on the evaluator port: refused', True, got != 'opened' and 'evaluat' in got)
    with st.connect['writer']() as db:
        try:
            db.execute('select authz.nexloop_evo_evaluation_open(%s,%s,%s)', (_digest_of(st.cfg['credentials']['writer']), c, bd))
            direct = 'opened'
        except psycopg.Error as error:
            direct = error.sqlstate
    r.check('writer role calling the evaluation entry: no privilege', '42501', direct)
    opened = st.evaluator.open(c, bd)
    r.check('evaluator opens (positive control)', True, opened['opened'])
    try:
        st.evaluator.l1_open(c, _digest_of(st.cfg['credentials']['writer']))
        l1 = 'opened'
    except Exception as error:  # noqa: BLE001
        l1 = refusal(error)
    r.check('a non-test credential cannot open L1', True, l1 != 'opened' and 'test-only evaluation credential' in (l1 or ''))
    session = st.evaluator.l1_open(c, _digest_of(st.cfg['credentials']['evaluation']))
    r.check('the registered test credential opens L1 (positive control)', True, session['opened'])
    st.evaluator.l1_close(session['session_id'])
    if st.registrar:
        with st.registrar() as db:
            try:
                with db.transaction():
                    db.execute("insert into authz.nexloop_evo_evaluation_credentials(token_digest,tenant_id,registered_by) values(%s,%s,'s6-stage-negative')",
                               (_digest_of(st.cfg['credentials']['writer']), st.inputs()['tenant']))
                reg = 'registered'
            except psycopg.Error as error:
                reg = error.diag.message_primary
        r.check('registering a non-test evaluation credential is refused', True, reg != 'registered' and 'world test only' in reg)
    else:
        r.notes.append('registration negative skipped: no registrar connection configured (the trusted configuration registers credentials)')


def at048(st, r):
    """AT-048: (a) a split whose groups put one Consumer's Runs on both sides; (b) two near-duplicate Runs (the same terms) across the
    sides: each opening returns leakage_detected; regrouped by Consumer (the near duplicates in one cluster) the opening passes."""
    i = st.inputs()
    units = st.holdout_units('at048')
    base = st.plan('at048-by-consumer', units)
    plans = {}
    paired = {x for p in i['pairs'] for x in p}
    every = base.candidate['design_case_ids'] + base.candidate['validation_case_ids']
    # A validation Run (not a near duplicate) whose Consumer has another Run (not a near duplicate either): that one goes to design.
    v0, same = None, []
    for x in base.candidate['validation_case_ids']:
        others = [y for y in every if i['clusters'][y] == i['clusters'][x] and y != x and y not in paired]
        if x not in paired and others:
            v0, same = x, others
            break
    if same:
        # One Consumer's Runs on both sides, each Run its own group; the near duplicates are left out (only the Consumer leaks).
        runs = [x for x in base.candidate['design_case_ids'] + base.candidate['validation_case_ids'] if x not in paired]
        val = sorted(set(base.candidate['validation_case_ids']) - set(same) - paired)
        if val and set(runs) - set(val):
            plans['by_run'] = st.plan('at048-by-run', units, groups={x: 'run:' + x for x in runs}, design=sorted(set(runs) - set(val)), validation=val)
    if i['pairs']:
        a, b = i['pairs'][0]
        runs = sorted(base.candidate['design_case_ids'] + base.candidate['validation_case_ids'])
        firsts = {}
        for x in runs:  # one Run of each other Consumer (not the pair's, not near duplicates)
            if x not in paired and i['clusters'][x] not in (i['clusters'][a], i['clusters'][b]):
                firsts.setdefault(i['clusters'][x], x)
        other = [firsts[c] for c in sorted(firsts)]
        if len(other) >= 2:
            groups = {x: ('split:' + x if x in (a, b) else i['clusters'][x]) for x in runs}
            plans['near_duplicate'] = st.plan('at048-near-duplicate', units, groups=groups, design=sorted([a, other[0]]), validation=sorted([b, other[1]]))
            merged = {x: ('cluster:near-duplicates' if x in (a, b) else i['clusters'][x]) for x in runs}
            plans['regrouped'] = st.plan('at048-regrouped', units, groups=merged, design=sorted([a, b, other[0]]), validation=[other[1]])
    r.step('plan', bool(plans), routes=sorted(plans), near_duplicate_pairs=len(i['pairs']))
    if not same:
        r.notes.append('route by_run not planned: no Consumer with two Runs in the holdout')
    if not i['pairs']:
        r.notes.append('route near_duplicate not planned: no two Runs of different Consumers asked the same terms in the window')
    if r.dry_run:
        return
    for route in ('by_run', 'near_duplicate'):
        if route in plans:
            _, opened = st.freeze_open(plans[route], 'at048-' + route)
            r.check(f'{route}: opening returns leakage_detected', 'leakage_detected', opened.get('reason') if not opened['opened'] else 'opened')
    for route, plan in (('by_consumer', base), ('regrouped', plans.get('regrouped'))):
        if plan is not None:
            _, opened = st.freeze_open(plan, 'at048-' + route)
            r.check(f'{route}: opening passes', True, opened['opened'])


def r4(st, r):
    """S6-R4: the sixth candidate on one holdout returns holdout_exhausted (five are decided at most, D39-3); a dimension that is not
    open is refused (propose; the evaluation-open code dimension_closed needs a closed dimension the services cannot create)."""
    units = st.holdout_units('r4')
    drafts = [st.plan(f'r4-{n}', units) for n in range(1, 7)]
    uses = st.holdout_uses(drafts[0].candidate['validation_case_ids'])
    with st.inspect() as db:
        open_dims = sorted(x[0] for x in db.execute('select dimension from control.nexloop_evo_open_dimensions'))
    r.step('plan', uses == 0, candidates=len(drafts), holdout_uses=uses, open_dimensions=open_dims)
    if uses:
        r.notes.append('the R4 holdout already has uses: a rerun needs a new label and new synthetic data')
    if r.dry_run:
        return
    opened = [st.freeze_open(d, f'r4-{n}')[1] for n, d in enumerate(drafts, 1)]
    r.check('candidates 1-5 open with holdout uses 1-5', [(True, n) for n in range(1, 6)], [(o['opened'], o.get('holdout_use')) for o in opened[:5]])
    r.check('candidate 6: holdout_exhausted', (False, 'holdout_exhausted'), (opened[5]['opened'], opened[5].get('reason')))
    r.check('candidate 6 stays proposed', ['proposed', None], list(st.candidate_state(drafts[5].candidate['candidate_id'])))
    closed = st.plan('r4-closed-dimension', units, dimension='semantic_tool')
    try:
        st.propose(closed)
        got = 'proposed'
    except Exception as error:  # noqa: BLE001
        got = refusal(error)
    r.check('a closed dimension is refused', 'evolution dimension semantic_tool not open', got)


def at023(st, r):
    """AT-023: one L0 evaluation, one L1 evaluation (world test) and one rejected candidate; the protected tables do not change."""
    units = st.holdout_units('at023')
    plans = {k: st.plan('at023-' + k, units) for k in ('l0', 'l1', 'rejected')}
    r.step('plan', True, candidates={k: p.candidate['candidate_id'] for k, p in plans.items()}, holdout_uses=st.holdout_uses(plans['l0'].candidate['validation_case_ids']))
    r.digests['before'] = st.digests()
    if r.dry_run:
        return
    frozen, opened = st.freeze_open(plans['l0'], 'at023-l0')
    report = st.l0(frozen, plans['l0'])
    out = st.evaluator.conclude(report)
    r.check('L0: accepted_for_release', ('accept', 'accepted_for_release'), (report['conclusion'], out['status']))
    frozen, opened = st.freeze_open(plans['l1'], 'at023-l1', levels=('L0', 'L1'))
    session = st.evaluator.l1_open(plans['l1'].candidate['candidate_id'], _digest_of(st.cfg['credentials']['evaluation']))
    snapshot = st.evaluator.l1_definitions(session['session_id'])
    l0 = st.l0(frozen, plans['l1'])
    cases = frozen['bundle']['design_case_ids'] + frozen['bundle']['validation_case_ids']
    l1 = run_l1(frozen['bundle'], frozen['bundle_digest'], l0, [{'case_id': c, 'origin': 'synthetic'} for c in cases], synthetic_runner,
                reserve=lambda cid, amount: st.evaluator.l1_reserve(session['session_id'], cid, amount, 'CNY')['reserved'],
                now=lambda: 0, deadline=1, created_at=st.cfg['created_at'])
    out = st.evaluator.conclude(l1)
    closed = st.evaluator.l1_close(session['session_id'])
    r.check('L1: snapshot taken, accepted, session closed with the snapshot removed', (True, 'accept', 'accepted_for_release', 'closed', 0),
            (bool(snapshot), l1['conclusion'], out['status'], closed['status'], closed['remaining']))
    frozen, opened = st.freeze_open(plans['rejected'], 'at023-rejected')
    report = st.l0(frozen, plans['rejected'], inputs={})  # latency not measured: G7 fails (missing measurements never pass)
    out = st.evaluator.conclude(report)
    r.check('rejected: hard gate failed (cost_performance), status rejected', (['cost_performance'], 'hard_gate_failed', 'rejected'),
            (report['failed_gates'], report['reason'], out['status']))
    r.digests['after'] = st.digests()
    r.check('protected tables unchanged', r.digests['before'], r.digests['after'])


def r1(st, r):
    """S6-R1: the same frozen input evaluated twice gives the same L0 report digest; the stored conclusion replays it."""
    draft = st.plan('r1', st.holdout_units('r1'))
    r.step('plan', True, candidate=draft.candidate['candidate_id'])
    if r.dry_run:
        return
    frozen, opened = st.freeze_open(draft, 'r1')
    first, second = st.l0(frozen, draft), st.l0(frozen, draft)
    r.check('two L0 runs, one report digest', E.report_digest(first), E.report_digest(second))
    r.check('each run reproduced itself (D39-2)', (True, True), (first['reproduced'], second['reproduced']))
    out = st.evaluator.conclude(first)
    again = st.evaluator.conclude(second)
    r.check('the second run concludes as a replay of the first, under the same stored digest', (True, out.get('report_digest')),
            (again.get('replayed'), again.get('report_digest')))


def r3(st, r):
    """S6-R3: the candidate with the highest score but a failed G5 (a protected ref pushed out) is rejected."""
    i = st.inputs()
    protected = set(st.cfg['protected_refs'])
    units = st.holdout_units('r3')
    base = st.plan('r3-g5', units)
    scope = base.candidate['allowed_change_scope']
    target = None
    for ref in scope:
        patch = {**base.patch, 'target_refs': [ref]}
        if any(protected & (set(E._results(a, None)) - set(E._results(a, patch))) for a in i['accesses'] if a.get('kind') == 'access'
               and a['run_id'] in base.candidate['design_case_ids'] + base.candidate['validation_case_ids']):
            target = ref
            break
    r.step('plan', target is not None, protected_refs=len(protected), target=target)
    if target is None:
        raise Precondition('no scoped definition whose alias pushes a configured protected ref past the browse limit in the R3 cases')
    g5 = st.plan('r3-g5', units, patch={'target_refs': [target]})
    plain = st.plan('r3-plain', units, patch={'target_refs': [target], 'term_hashes': []})
    if r.dry_run:
        return
    inputs = {**st.cfg['l0_inputs'], 'protected_refs': sorted(protected)}
    reports = {}
    for name, d in (('g5', g5), ('plain', plain)):
        frozen, opened = st.freeze_open(d, 'r3-' + name)
        reports[name] = (st.l0(frozen, d, inputs=inputs), d)
    score = {k: rep['scores']['validation']['candidate'] - rep['scores']['validation']['parent'] for k, (rep, _) in reports.items()}
    rep = reports['g5'][0]
    r.check('the G5 candidate has the highest score', 'g5', max(score, key=lambda k: (score[k], k == 'g5')))
    r.check('G5 failed', True, 'no_forbidden_regression' in rep['failed_gates'])
    out = st.evaluator.conclude(rep)
    r.check('rejected (hard_gate_failed), status rejected', ('reject', 'hard_gate_failed', 'rejected'), (rep['conclusion'], rep['reason'], out['status']))


def _state(st):
    return Path(st.cfg['state_dir']) / 'r7.json'


def r7_prepare(st, r):
    """S6-R7 (1/2): a candidate whose cases include the configured synthetic Consumer, evaluating, with an L1 snapshot."""
    consumer = st.cfg['erasure']['consumer_id']
    i = st.inputs()
    if consumer is None or consumer not in {s.get('consumer_id') for s in i['samples']}:
        raise Precondition('erasure.consumer_id: a synthetic Consumer whose Runs are in the window')
    draft = st.plan('r7', st.holdout_units('r7'))
    runs = [s['run_id'] for s in i['samples'] if s.get('consumer_id') == consumer]
    r.step('plan', bool(set(runs) & set(draft.candidate['design_case_ids'] + draft.candidate['validation_case_ids'])), candidate=draft.candidate['candidate_id'])
    if r.dry_run:
        return
    frozen, opened = st.freeze_open(draft, 'r7', levels=('L0', 'L1'))
    session = st.evaluator.l1_open(draft.candidate['candidate_id'], _digest_of(st.cfg['credentials']['evaluation']))
    state = {'candidate_id': draft.candidate['candidate_id'], 'session_id': session['session_id'], 'holdout_digest': opened.get('holdout_digest'),
             'consumer_id': consumer, 'runs': sorted(set(runs) & set(draft.candidate['design_case_ids'] + draft.candidate['validation_case_ids']))}
    path = _state(st)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream)
    r.check('evaluating with an L1 snapshot', ('evaluating', True), (st.candidate_state(state['candidate_id'])[0], bool(st.evaluator.l1_definitions(session['session_id']))))
    r.notes.append('next: the owner erases the synthetic Consumer (scripts/deploy/erase_synthetic_consumer.py erase), then r7-verify')


def r7_verify(st, r):
    """S6-R7 (2/2): after the erasure, the holdout is invalidated (missing_data), the candidate incomplete, the snapshot gone."""
    try:
        state = json.loads(read_private_text(_state(st), maximum=65536))
    except (OSError, ValueError, PermissionError):
        raise Precondition('r7-prepare first (no private state)') from None
    with st.inspect() as db:
        erasing = db.execute("select count(*) from control.nexloop_consumer_erasures where consumer_id=%s", (state['consumer_id'],)).fetchone()[0]
        invalid = [x[0] for x in db.execute('select reason from runtime.nexloop_evo_holdout_invalidations where holdout_digest=%s', (state['holdout_digest'],))]
        session = db.execute('select status,close_reason from runtime.nexloop_evo_l1_sessions where session_id=%s', (state['session_id'],)).fetchone()
        snapshot = db.execute('select count(*) from runtime.nexloop_evo_l1_definitions where session_id=%s', (state['session_id'],)).fetchone()[0]
        refs = db.execute('select count(*) from runtime.nexloop_evo_case_refs where run_id::text = any(%s)', (state['runs'],)).fetchone()[0]
    r.step('probe', erasing > 0, erasure_recorded=erasing)
    if r.dry_run:
        return
    r.check('holdout invalidated: missing_data', True, 'missing_data' in invalid)
    r.check('candidate incomplete: missing_data', ['incomplete', 'missing_data'], list(st.candidate_state(state['candidate_id'])))
    r.check('L1 session closed with the candidate, snapshot removed', (['closed', 'candidate_ended'], 0), (list(session) if session else None, snapshot))
    r.check("the erased Consumer's Runs left the case references", 0, refs)


COMMANDS = {'at046': at046, 'r5': r5, 'at048': at048, 'r4': r4, 'at023': at023, 'r1': r1, 'r3': r3, 'r7-prepare': r7_prepare, 'r7-verify': r7_verify}
ALL = ('at046', 'r5', 'at048', 'r4', 'at023', 'r1', 'r3')  # the checklist's order (§4.3); r7 needs the owner's erasure in between


def candidate_template(st, r):
    """For L2's NX-040 checks: an open candidate body of this diagnosis (its own holdout), written 0600 to inputs.candidate_template."""
    path = st.cfg.get('candidate_template')
    if not path:
        raise Precondition('inputs.candidate_template: the path to write is not configured')
    draft = st.plan('template', st.holdout_units('template'))
    body = {'diagnosis_id': diagnosis_id(st.inputs()['diagnosis'].snapshot_digest), 'candidate': draft.candidate, 'category': draft.category,
            'patch': draft.patch, 'case_groups': draft.case_groups, 'regroups': draft.regroups, 'source_consumer_ids': draft.source_consumer_ids}
    r.step('plan', True, candidate=draft.candidate['candidate_id'])
    if r.dry_run:
        return
    st.writer.open_diagnosis(st.inputs()['diagnosis'], st.inputs()['samples'], parent_revision=st.cfg['parent_revision'],
                             evaluator_version=st.cfg['evaluator_version'], model_profile=st.cfg['model_profile'])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(body, stream)
    r.check('candidate template written (the diagnosis is open)', True, os.path.isfile(path))


COMMANDS['candidate-template'] = candidate_template


def run_command(stage, config, command, dry_run, out):
    r = Result(command, config, dry_run)
    try:
        stage.roles(r)
        if command != 'at046' and not command.startswith('r7'):
            i = stage.inputs()
            r.step('inputs', True, runs=len(i['samples']), accesses=len(i['accesses']), clusters=len(set(i['clusters'].values())), units=len(i['units']),
                   excluded=i['excluded'])
        COMMANDS[command](stage, r)
    except Precondition as error:
        r.block('precondition', str(error))
    except psycopg.Error as error:
        r.res.step('database refusal', None, C.refusal(error), False)
    return r.write(out)


def main(argv=None, out=sys.stdout):
    p = argparse.ArgumentParser(description='S6 stage acceptance runs (NX-038 / NX-039)')
    p.add_argument('check', choices=sorted(COMMANDS) + ['all'])
    p.add_argument('--config', required=True)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    try:
        config, ev = load_config(a.config)
    except Refused as error:
        print(json.dumps({'refused': str(error)}), file=out)
        return 2
    stage = Stage(ev)
    codes = [run_command(stage, config, c, a.dry_run, out) for c in (ALL if a.check == 'all' else (a.check,))]
    return max(codes)


if __name__ == '__main__':
    sys.exit(main())
