"""Decision-evaluation review layer: client of the Ark "System One" decision API (beta), not wired into any runtime path.

Design: docs/implementation/decision-eval-system-one-design.md (owner-approved pre-trial implementation of the parts that do not
go live; stays on its branch until the 10/22 acceptance). A judgment is evidence or a hypothesis only: nothing here changes a
business object, and the caller decides through governed EIOS Actions.

- Request: POST <endpoint> with `ark-beta-systemone-api: true`, a Bearer credential read per call from a private file
  (read_private_text: 0600, owned, regular; never from the environment or .env), body {model, state, questions}.
- Every failure is `uncertain`, never an exception to the caller: transport error, timeout, HTTP error, a body that is not JSON
  or whose answers do not validate strictly (noul value in [0,1]; choice in the declared options, probabilities over exactly those
  options summing to 1 within 1e-3, the chosen one the most probable; score a number inside the scale with probabilities and legend
  over exactly the scale's keys), a missing question, an exhausted deadline. `confidence` is recorded, never cross-checked.
- Retries: transport errors, timeouts and 5xx only, exponential backoff with jitter inside one absolute deadline; 4xx never.
- Cache key: sha256 of the canonical state + question-set version + model id; an in-memory cache of validated results only.
- No credential, state text or response body is ever logged or returned; results carry numbers, codes and digests.
"""
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import math
import random
import re
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from nexloop_eios.private_configuration import read_private_text

QUESTIONS_SCHEMA = 'nexloop-decision-questions/1'
BETA_HEADER = ('ark-beta-systemone-api', 'true')
TYPES = ('noul', 'choice', 'score')
_ID = re.compile(r'[a-z][a-z0-9_]{0,63}')
_VERSION = re.compile(r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.v[1-9][0-9]*')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_CREDENTIAL = re.compile(r'[\x21-\x7e]{8,4096}')
# A choice option is the label exactly as the criteria writes it and the API returns it (v2: e.g. a Chinese label); no mapping.
_OPTION = re.compile(r'[^\x00-\x1f\x7f:,]{1,64}')
LEGACY_INSUFFICIENT = 'insufficient_information'  # v1 has no `insufficient` field; its label is this one
CONFIGURATION = Path(__file__).resolve().parents[4] / 'deploy/configuration'
MAX_STATE_BYTES = 65536


class QuestionsInvalid(ValueError):
    pass


# Question definitions ---------------------------------------------------------------------------------------------------
def validate_questions(body):
    """The versioned question file (deploy/configuration/decision-questions.v1.json); returns it or raises QuestionsInvalid."""
    def fail(reason):
        raise QuestionsInvalid(reason)
    if type(body) is not dict or body.get('schema') != QUESTIONS_SCHEMA or type(body.get('version')) is not int or type(body.get('sets')) is not dict:
        fail('questions file shape')
    if not body['sets']:
        fail('no question set')
    for name, qset in body['sets'].items():
        if not _ID.fullmatch(name) or type(qset) is not dict or not set(qset) <= {'version', 'purpose', 'questions', 'thresholds'}:
            fail(f'set {name}')
        if type(qset.get('version')) is not str or not _VERSION.fullmatch(qset['version']) or not qset['version'].startswith(name + '.'):
            fail(f'set {name}: version')
        questions, thresholds = qset.get('questions'), qset.get('thresholds')
        if type(questions) is not dict or not questions or type(thresholds) is not dict or set(thresholds) != set(questions):
            fail(f'set {name}: questions and thresholds')
        for qid, q in questions.items():
            if not _ID.fullmatch(qid) or type(q) is not dict or q.get('type') not in TYPES:
                fail(f'{name}.{qid}: type')
            if type(q.get('version')) is not str or not _VERSION.fullmatch(q['version']) or not q['version'].startswith(f'{name}.{qid}.'):
                fail(f'{name}.{qid}: version')
            for text in ('instructions', 'criteria'):
                if type(q.get(text)) is not str or not 1 <= len(q[text]) <= 2000:
                    fail(f'{name}.{qid}: {text}')
            allowed = {'version', 'type', 'instructions', 'criteria'} | ({'options', 'insufficient'} if q['type'] == 'choice' else set()) \
                | ({'scale', 'legend'} if q['type'] == 'score' else set())
            if not set(q) <= allowed:
                fail(f'{name}.{qid}: keys')
            t = thresholds[qid]
            if q['type'] == 'noul':
                if type(t) is not dict or set(t) != {'low', 'high'} or not all(type(t[k]) in (int, float) for k in t) or not 0 <= t['low'] < t['high'] <= 1:
                    fail(f'{name}.{qid}: noul thresholds')
            elif q['type'] == 'choice':
                options = q.get('options')
                if type(options) is not list or not 2 <= len(options) <= 16 or len(set(options)) != len(options) \
                        or not all(type(o) is str and o == o.strip() and _OPTION.fullmatch(o) for o in options):
                    fail(f'{name}.{qid}: options')
                if 'insufficient' in q and q['insufficient'] not in options:
                    fail(f'{name}.{qid}: insufficient must be one of the options')
                if body['version'] >= 2:
                    # v2 and later: every option is spelled in the criteria exactly as declared (the API answers with it).
                    if 'insufficient' not in q or not all(o in q['criteria'] for o in options):
                        fail(f'{name}.{qid}: v2 options must be written in the criteria and name the insufficient label')
                if type(t) is not dict or set(t) != {'confidence'} or type(t['confidence']) not in (int, float) or not 0 < t['confidence'] <= 1:
                    fail(f'{name}.{qid}: choice threshold')
            else:
                scale = q.get('scale')
                if type(scale) is not list or len(scale) != 2 or not all(type(v) is int for v in scale) or not 0 <= scale[0] < scale[1] <= 100:
                    fail(f'{name}.{qid}: scale')
                legend = q.get('legend')
                if type(legend) is not dict or set(legend) != {str(v) for v in range(scale[0], scale[1] + 1)}:
                    fail(f'{name}.{qid}: legend')
                if (type(t) is not dict or set(t) != {'handoff_at', 'confidence'} or type(t['handoff_at']) is not int
                        or not scale[0] <= t['handoff_at'] <= scale[1] or type(t['confidence']) not in (int, float) or not 0 < t['confidence'] <= 1):
                    fail(f'{name}.{qid}: score thresholds')
    return body


def load_questions(path):
    try:
        return validate_questions(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as error:
        raise QuestionsInvalid(str(error) if isinstance(error, QuestionsInvalid) else 'questions file unreadable') from None


def questions_path(version, root=CONFIGURATION):
    if type(version) is not int or not 1 <= version <= 999:
        raise QuestionsInvalid('question file version')
    return Path(root) / f'decision-questions.v{version}.json'


def load_questions_version(version, root=CONFIGURATION):
    """The question file of exactly this version (decision-questions.v<N>.json whose `version` is N). Earlier versions stay
    loadable unchanged so that past judgments can be reproduced."""
    body = load_questions(questions_path(version, root))
    if body['version'] != version:
        raise QuestionsInvalid('question file version mismatch')
    return body


def insufficient_label(question):
    """The choice label that means "not enough information": declared in v2+, the legacy label in v1."""
    return question.get('insufficient', LEGACY_INSUFFICIENT)


# Request and response --------------------------------------------------------------------------------------------------
def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def state_digest(state):
    return hashlib.sha256(canonical(state).encode()).hexdigest()


def cache_key(state, set_version, model):
    """sha256(canonical state) + question-set version + model id (design §4)."""
    return f'{state_digest(state)}:{set_version}:{model}'


def request_body(qset, state, model):
    """The body sent: {model, state, questions}; per question only type, instructions and criteria (versions stay local)."""
    return {'model': model, 'state': state if type(state) is str else canonical(state),
            'questions': {qid: {'type': q['type'], 'instructions': q['instructions'], 'criteria': q['criteria']}
                          for qid, q in qset['questions'].items()}}


def _probability(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


SUM_TOLERANCE = 1e-3


def _distribution(probabilities, keys):
    """Probabilities over exactly `keys` (any order), each in [0,1], summing to 1 within SUM_TOLERANCE."""
    if type(probabilities) is not dict or set(probabilities) != set(keys) or not all(_probability(p) for p in probabilities.values()):
        return False
    return abs(sum(probabilities.values()) - 1) <= SUM_TOLERANCE


def validate_answer(question, answer):
    """A strictly validated answer reduced to numbers, or None when anything is off. Shapes as observed on 2026-10-15:
    noul {"type":"noul","noul":p}; choice {"type","choice","probabilities","confidence"} where confidence is recorded only (it is
    not the chosen option's probability); score {"type","score" (weighted, not an integer),"probabilities" (keys "0".."n", any
    order),"confidence","legend"}."""
    if type(answer) is not dict:
        return None
    kind = question['type']
    if 'type' in answer and answer['type'] != kind:
        return None
    if kind == 'noul':
        p = answer.get('noul')
        return {'type': 'noul', 'probability': float(p)} if _probability(p) else None
    confidence = answer.get('confidence')
    if confidence is not None and not _probability(confidence):
        return None
    if kind == 'choice':
        options, choice, probs = question['options'], answer.get('choice'), answer.get('probabilities')
        if choice not in options or not _distribution(probs, options):
            return None
        if probs[choice] < max(probs.values()) - 1e-12:
            return None
        return {'type': 'choice', 'choice': choice, 'choice_probability': float(probs[choice]),
                'confidence': None if confidence is None else float(confidence), 'probabilities': {k: float(v) for k, v in probs.items()}}
    low, high = question['scale']
    keys = [str(v) for v in range(low, high + 1)]
    score, probs, legend = answer.get('score'), answer.get('probabilities'), answer.get('legend')
    if type(score) not in (int, float) or isinstance(score, bool) or not math.isfinite(score) or not low <= score <= high:
        return None
    if not _distribution(probs, keys):
        return None
    if legend is not None and (type(legend) is not dict or set(legend) != set(keys)):
        return None
    level = max(keys, key=lambda k: probs[k])
    return {'type': 'score', 'score': float(score), 'level': int(level), 'level_probability': float(probs[level]),
            'confidence': None if confidence is None else float(confidence), 'probabilities': {k: float(v) for k, v in probs.items()}}


def parse_response(qset, payload):
    """{qid: answer} when every question of the set is answered and valid; else None."""
    if type(payload) is not dict or type(payload.get('answers')) is not dict:
        return None
    answers = {}
    for qid, question in qset['questions'].items():
        answer = validate_answer(question, payload['answers'].get(qid))
        if answer is None:
            return None
        answers[qid] = answer
    return answers


def _usage(payload):
    usage = payload.get('usage') if type(payload) is dict else None
    out = {}
    if type(usage) is dict:
        for key in ('input_tokens', 'output_tokens'):  # output_tokens is 0 for judgment-only answers; that is normal
            if type(usage.get(key)) is int and usage[key] >= 0:
                out[key] = usage[key]
    return out


# Transport ---------------------------------------------------------------------------------------------------------------
class TransportError(Exception):
    pass


def https_transport(url, headers, body, timeout):
    """POST over TLS with the system CA store; returns (status, body bytes). Raises TransportError on any connection problem."""
    parts = urlsplit(url)
    try:
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=ssl.create_default_context())
        try:
            connection.request('POST', parts.path or '/', body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(1 << 20)
        finally:
            connection.close()
    except (OSError, http.client.HTTPException) as error:
        raise TransportError(type(error).__name__) from None


# Client ----------------------------------------------------------------------------------------------------------------
@dataclass
class Result:
    status: str                       # 'ok' or 'uncertain'
    set_version: str
    model: str
    state_digest: str
    cache_key: str
    answers: dict = field(default_factory=dict)
    reason: str | None = None         # code only (timeout, http_4xx, http_5xx, invalid_response, transport, deadline, ...)
    attempts: int = 0
    latency_ms: float = 0.0
    usage: dict = field(default_factory=dict)
    cache_hit: bool = False


class DecisionEvalClient:
    def __init__(self, *, endpoint, credential_file, model, timeout=5.0, retries=3, backoff=1.0, transport=https_transport,
                 sleep=time.sleep, clock=time.monotonic, jitter=random.random, cache=None):
        parts = urlsplit(endpoint)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('decision_eval_endpoint_invalid')
        if not _MODEL.fullmatch(model or ''):
            raise ValueError('decision_eval_model_invalid')
        if not (type(timeout) in (int, float) and 0 < timeout <= 30) or not (type(retries) is int and 0 <= retries <= 5):
            raise ValueError('decision_eval_limits_invalid')
        self.endpoint, self.credential_file, self.model = endpoint, Path(credential_file), model
        self.timeout, self.retries, self.backoff = float(timeout), retries, float(backoff)
        self.transport, self.sleep, self.clock, self.jitter = transport, sleep, clock, jitter
        self.cache = {} if cache is None else cache

    def _credential(self):
        value = read_private_text(self.credential_file, maximum=4096)
        if not _CREDENTIAL.fullmatch(value):
            raise ValueError('credential')
        return value

    def evaluate(self, qset, state):
        """Never raises; never blocks beyond the deadline (timeout per attempt, all attempts within one absolute deadline)."""
        started = self.clock()
        version = qset.get('version', '') if type(qset) is dict else ''
        digest = state_digest(state)
        key = cache_key(state, version, self.model)
        result = Result(status='uncertain', set_version=version, model=self.model, state_digest=digest, cache_key=key)
        cached = self.cache.get(key)
        if cached is not None:
            return Result(**{**cached.__dict__, 'cache_hit': True, 'attempts': 0, 'latency_ms': 0.0})
        try:
            body = canonical(request_body(qset, state, self.model)).encode()
            if len(body) > MAX_STATE_BYTES:
                result.reason = 'state_too_large'
                return result
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._credential(), BETA_HEADER[0]: BETA_HEADER[1]}
        except Exception:
            result.reason = 'configuration'
            return result
        deadline = started + self.timeout * (self.retries + 1) + self.backoff * (2 ** self.retries)
        for attempt in range(self.retries + 1):
            result.attempts = attempt + 1
            remaining = deadline - self.clock()
            if remaining <= 0:
                result.reason = 'deadline'
                break
            retry = False
            try:
                status, raw = self.transport(self.endpoint, headers, body, min(self.timeout, remaining))
                if 500 <= status <= 599:
                    result.reason, retry = 'http_5xx', True
                elif status != 200:
                    result.reason = 'http_4xx' if 400 <= status <= 499 else 'http_status'
                else:
                    try:
                        payload = json.loads(raw)
                    except (ValueError, TypeError):
                        payload = None
                    answers = parse_response(qset, payload)
                    if answers is None:
                        result.reason = 'invalid_response'
                    else:
                        result.status, result.answers, result.reason, result.usage = 'ok', answers, None, _usage(payload)
            except TimeoutError:
                result.reason, retry = 'timeout', True
            except TransportError as error:
                result.reason, retry = ('timeout' if 'imeout' in str(error) else 'transport'), True
            except Exception:
                result.reason = 'client_error'
            if not retry or attempt == self.retries:
                break
            pause = self.backoff * (2 ** attempt) * (0.5 + self.jitter() / 2)
            if self.clock() + pause >= deadline:
                result.reason = 'deadline'
                break
            self.sleep(pause)
        result.latency_ms = round((self.clock() - started) * 1000, 1)
        if result.status == 'ok':
            self.cache[key] = result
        return result


# Decisions and evidence ------------------------------------------------------------------------------------------------
def verdicts(qset, result):
    """Per question: 'yes' | 'no' | 'between' (to a human) | 'uncertain'; choice and score give their value or 'between'."""
    if result.status != 'ok':
        return {qid: 'uncertain' for qid in qset['questions']}
    out = {}
    for qid, question in qset['questions'].items():
        t, a = qset['thresholds'][qid], result.answers[qid]
        if question['type'] == 'noul':
            out[qid] = 'yes' if a['probability'] >= t['high'] else 'no' if a['probability'] <= t['low'] else 'between'
        elif question['type'] == 'choice':
            # The chosen option's own probability decides; the response's confidence is recorded only.
            out[qid] = a['choice'] if a['choice_probability'] >= t['confidence'] else 'between'
        else:
            # The most probable level decides (score itself is a weighted value, not a level).
            out[qid] = a['level'] if a['level_probability'] >= t['confidence'] else 'between'
    return out


def decide(set_name, qset, result, *, extracted_kind=None):
    """(decision, reason_code). claim_review: pass | needs_human | uncertain; handoff: handoff | continue | uncertain. Anything
    between thresholds, failed or invalid is `uncertain` (to a human); never an automatic accept on doubt."""
    v = verdicts(qset, result)
    if result.status != 'ok':
        return 'uncertain', result.reason or 'uncertain'
    if set_name == 'claim_review':
        if v.get('injection') == 'yes':
            return 'needs_human', 'injection_suspected'
        if v.get('grounded') == 'no':
            return 'needs_human', 'ungrounded'
        kind = v.get('kind')
        insufficient = insufficient_label(qset['questions']['kind']) if 'kind' in qset['questions'] else LEGACY_INSUFFICIENT
        if v.get('grounded') == 'yes' and v.get('injection') == 'no' and kind not in ('between', insufficient) \
                and (extracted_kind is None or kind == extracted_kind):
            return 'pass', 'grounded'
        return 'uncertain', 'uncertain'
    if set_name == 'handoff':
        risk = v.get('risk')
        threshold = qset['thresholds'].get('risk', {}).get('handoff_at')
        if v.get('needs_human') == 'yes' or (type(risk) is int and threshold is not None and risk >= threshold):
            return 'handoff', 'decision_eval_handoff'
        if v.get('needs_human') == 'no' and type(risk) is int:
            return 'continue', 'no_handoff'
        return 'uncertain', 'uncertain'
    return 'uncertain', 'unknown_set'


def evidence_record(*, set_name, qset, result, decision, reason_code, subject_kind=None, subject_id=None, thresholds_version=None):
    """Design §5 record (pure; no I/O, no state text, no reasoning): digest, question-set and question versions, model id,
    probabilities, thresholds, decision and reason, usage and timing."""
    return {
        'subject_kind': subject_kind, 'subject_id': subject_id,
        'state_digest': result.state_digest, 'question_set': qset['version'],
        'question_versions': {qid: q['version'] for qid, q in qset['questions'].items()},
        'model_id': result.model, 'provider': 'ark-systemone-beta',
        'answers': result.answers if result.status == 'ok' else {},
        'thresholds': {'version': thresholds_version or qset['version'], 'values': qset['thresholds']},
        'decision': decision, 'reason_code': reason_code,
        'status': result.status, 'failure': result.reason if result.status != 'ok' else None,
        'usage': result.usage, 'latency_ms': result.latency_ms, 'attempts': result.attempts, 'cache_hit': result.cache_hit,
        'cache_key': result.cache_key,
    }
