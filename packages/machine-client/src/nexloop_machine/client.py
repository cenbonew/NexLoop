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
        saved_params = json.dumps(params, allow_nan=False)
        value = self._request("POST", "prepare", {"capability": cap, "intent_id": intent_id, "params": params})
        digest = value.get("prepare_digest")
        if value.get("intent_id") != intent_id or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise MachineError("Invalid prepare response")
        self._prepared[cap, intent_id, digest] = saved_params
        return value

    def commit(self, cap, intent_id, prepare_digest):
        _intent(intent_id)
        params = self._prepared.pop((cap, intent_id, prepare_digest), None)
        if params is None:
            raise MachineError("Matching prepare required")
        body = {"capability": cap, "intent_id": intent_id, "params": json.loads(params),
                "prepare_digest": prepare_digest}
        try:
            value = self._request("POST", "commit", body)
        except MachineError as error:
            if error.status is not None and error.status < 500:
                raise
        else:
            if (value.get("intent_id") == intent_id and value.get("phase") in ("committed", "pending_approval", "refused")
                    and value.get("status") != "unknown"):
                return value
        return self.query(intent_id)

    def query(self, intent_id):
        _intent(intent_id)
        return self._request("GET", "intents/" + intent_id)
