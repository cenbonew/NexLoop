# NexLoop machine client

Install this standalone package with Python 3.12 or newer. The HTTP SDK and the local newline-delimited JSON-RPC stdio server use the standard library. Supply the deployment HTTPS origin and optional CA file. The client verifies TLS and does not follow redirects. Store the issued machine key in a regular file owned by the current user, mode 0600.

```python
from nexloop_machine import Client

client = Client(base_url, key_file, ca_file=ca_file)
available = client.capabilities()
result = client.read(verb, **params)
prepared = client.prepare(cap, intent_id, **params)
# Present the proposed action to the operator for confirmation before commit.
result = client.commit(cap, intent_id, prepared["prepare_digest"])
```

Prepare and commit share a Client instance. The SDK keeps the parameters in memory to satisfy the M5c commit wire format. On an ambiguous commit it queries the same intent once. If reconciliation fails, use `client.query(intent_id)` later. A conflict or rate limit is returned to the caller; for HTTP 429 respect `MachineError.retry_after` (seconds). There are no automatic write retries. A pending approval belongs to the human approval workflow.

Start `nexloop-machine-mcp --base-url "$BASE_URL" --ca-file "$CA_FILE"` with `NEXLOOP_MACHINE_KEY_FILE` set to the private file path. The server supports MCP 2024-11-05 initialize, ping, tools/list and tools/call. Each list and call obtains the current capability inventory. Read tools take an optional `params` object. Write prepare takes `intent_id` and optional `params`; commit takes `intent_id` and `prepare_digest`. Descriptions come from the machine API. The current wire inventory supplies name, kind and description only; the objects describe the transport envelope. The server enforces per-capability parameters and the current key scope and grants. No approval or key administration tools are exposed.
