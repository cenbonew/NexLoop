from datetime import datetime
import http.client
import json
import os
import re
import ssl
import stat
from urllib.parse import urlsplit


NEVER_GRANT_MARKERS = ('approval', 'machine_client', 'machine_key', 'service_grant', 'grant', 'credential', 'member', 'role', 'owner',
                       'publish', 'schema', 'policy', 'rule', 'relation_type', 'erase', 'erasure', 'retention', 'legal_hold',
                       'real_dispatch', 'channel', 'remediation', 'external_effect', 'nexloop.service.request',
                       'nexloop.enterprise.deliver', 'nexloop.service.query', 'nexloop.service.receipt_reconcile',
                       'nexloop.receipt.reconcile', 'schema.review')


def allowed_name(cap):
    return (isinstance(cap, str) and len(cap) <= 160
            and re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", cap) is not None
            and not any(marker in cap for marker in NEVER_GRANT_MARKERS))


class MachineError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _key(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if (stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_uid == os.geteuid()):
                raw = stream.read(256)
                if re.fullmatch(rb"nxm_[a-z2-7]{12}_[A-Za-z0-9_-]{43}\n?", raw):
                    return raw.rstrip(b"\n").decode("ascii")
    except (OSError, ValueError):
        pass
    raise MachineError("Invalid machine key file")


def _intent(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
        raise MachineError("Invalid intent ID")


def _intent_view(value, intent_id, capability=None):
    """Validate machine-intent.schema.json before trusting a readback or commit response."""
    required = {"intent_id", "capability", "phase", "result_code", "outcome_id",
                "approval_id", "expires_at", "committed_at"}
    if not required <= value.keys() <= required | {"replayed", "approval_status"}:
        raise MachineError("Invalid intent response fields")
    if (value["intent_id"] != intent_id or not allowed_name(value["capability"])
            or (capability is not None and value["capability"] != capability)):
        raise MachineError("Intent response mismatch")
    if (value["phase"] not in ("prepare", "pending_approval", "committed", "refused")
            or not isinstance(value["expires_at"], str)
            or type(value.get("replayed", False)) is not bool
            or value.get("approval_status") not in (None, "approved", "waiting_approval", "rejected", "cancelled", "expired")):
        raise MachineError("Invalid intent state")
    for field in ("committed_at", "approval_id", "result_code", "outcome_id"):
        if value[field] is not None and not isinstance(value[field], str):
            raise MachineError("Invalid intent field type")
    for field, pattern in (("result_code", "[a-z_:0-9A-Z]{1,80}"), ("outcome_id", "[0-9a-f]{64}")):
        if value[field] is not None and not re.fullmatch(pattern, value[field]):
            raise MachineError("Invalid intent result")
    if value["phase"] == "committed":
        if value["result_code"] != "succeeded" or value["outcome_id"] is None:
            raise MachineError("Inconsistent committed intent")
        try:
            stamp = datetime.fromisoformat(value["committed_at"])
        except (ValueError, TypeError):
            raise MachineError("Invalid intent commit time") from None
        if stamp.tzinfo is None:
            raise MachineError("Invalid intent commit time")
    if value["phase"] == "pending_approval" and not value["approval_id"]:
        raise MachineError("Missing approval reference")
    return value


class Client:
    def __init__(self, base_url, key_file, ca_file=None):
        try:
            base = urlsplit(base_url)
            if (base.scheme != "https" or not base.hostname or base.username is not None
                    or base.password is not None or base.query or base.fragment or base.path.strip("/")):
                raise ValueError
            self._host, self._port = base.hostname, base.port
            self._tls = ssl.create_default_context(cafile=ca_file)
        except (ValueError, OSError, TypeError):
            raise MachineError("Invalid HTTPS origin or CA") from None
        self._token = _key(key_file)
        self._prepared = {}
        self._pending = {}
        self._uncertain = set()

    def _exchange(self, method, path, payload):
        connection = http.client.HTTPSConnection(self._host, self._port, context=self._tls, timeout=15)
        try:
            connection.request(method, "/machine/v1/" + path, body=payload, headers={
                "Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.getheader("Retry-After"), response.read(1048577)
        finally:
            connection.close()

    def _request(self, method, path, body=None):
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        try:
            status, delay, raw = self._exchange(method, path, payload)
        except (OSError, http.client.HTTPException):
            raise MachineError("Transport unavailable") from None
        if not 200 <= status < 300:
            retry = int(delay) if status == 429 and delay and re.fullmatch(r"[0-9]{1,9}", delay) else None
            raise MachineError(f"Machine API HTTP {status}", status, retry)
        try:
            value = json.loads(raw) if len(raw) <= 1048576 else None
            if not isinstance(value, dict) or self._token in json.dumps(value, ensure_ascii=False):
                raise ValueError
            return value
        except (ValueError, RecursionError):
            raise MachineError("Invalid machine response") from None

    def capabilities(self):
        return self._request("GET", "capabilities")

    def read(self, verb, **params):
        if not allowed_name(verb):
            raise MachineError("Invalid capability")
        return self._request("POST", "read", {"verb": verb, "params": params})

    def prepare(self, cap, intent_id, **params):
        _intent(intent_id)
        if not allowed_name(cap):
            raise MachineError("Invalid capability")
        if intent_id in self._uncertain:
            raise MachineError("Query original intent only")
        saved_params = json.dumps(params, allow_nan=False)
        value = self._request("POST", "prepare", {"capability": cap, "intent_id": intent_id, "params": params})
        digest = value.get("prepare_digest")
        if value.get("intent_id") != intent_id or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise MachineError("Invalid prepare response")
        self._prepared[cap, intent_id, digest] = saved_params
        return value

    def commit(self, cap, intent_id, prepare_digest):
        _intent(intent_id)
        key = (cap, intent_id, prepare_digest)
        pending = self._pending.get(key)
        if pending is not None:
            approval_id, params = pending
            return self.resume_pending(cap, intent_id, prepare_digest, approval_id, json.loads(params))
        params = self._prepared.pop(key, None)
        if params is None:
            raise MachineError("Matching prepare required")
        return self._submit(cap, intent_id, prepare_digest, params)

    def resume_pending(self, cap, intent_id, prepare_digest, approval_id, params):
        """Explicit continuation using a trusted prepare request and pending approval receipt."""
        if (not allowed_name(cap) or not isinstance(approval_id, str) or not approval_id
                or not isinstance(prepare_digest, str) or not re.fullmatch("[0-9a-f]{64}", prepare_digest)
                or not isinstance(params, dict)):
            raise MachineError("Invalid pending approval reference")
        key = (cap, intent_id, prepare_digest)
        current = self._query(intent_id, cap)
        if intent_id in self._uncertain:
            return current
        if current["approval_id"] != approval_id:
            raise MachineError("Approval reference mismatch")
        if current["phase"] != "pending_approval" or current.get("approval_status") != "approved":
            return current
        self._pending.pop(key, None)
        return self._submit(cap, intent_id, prepare_digest, json.dumps(params, allow_nan=False))

    def _submit(self, cap, intent_id, digest, params):
        body = {"capability": cap, "intent_id": intent_id, "params": json.loads(params),
                "prepare_digest": digest}
        try:
            value = self._request("POST", "commit", body)
            _intent_view(value, intent_id, cap)
            if value["phase"] == "pending_approval":
                self._pending[cap, intent_id, digest] = (value["approval_id"], params)
            return value
        except MachineError as error:
            if error.status is not None and error.status < 500:
                raise
        # Ambiguous POST: only query. Its result must not implicitly arm another POST.
        self._uncertain.add(intent_id)
        return self._query(intent_id, cap)

    def _query(self, intent_id, capability=None):
        _intent(intent_id)
        return _intent_view(self._request("GET", "intents/" + intent_id), intent_id, capability)

    def query(self, intent_id):
        return self._query(intent_id)
