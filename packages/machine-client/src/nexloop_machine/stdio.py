"""Newline-delimited JSON-RPC over local stdio."""
import argparse
import json
import os
import re
import sys

from .client import Client, MachineError, allowed_name

VERSION = "2024-11-05"
INFO = {"name": "nexloop-machine-mcp", "version": "0.1.0"}


def catalog(client):
    tools = {}
    for row in client.capabilities()["capabilities"]:
        cap, kind, description = row["capability"], row["kind"], row["description"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", cap) or kind not in ("read", "write"):
            raise MachineError("Invalid capability catalog")
        if not allowed_name(cap):
            continue
        for op in (("read",) if kind == "read" else ("prepare", "commit")):
            name = cap if op == "read" else f"{cap}.{op}"
            fields = {"params": {"type": "object"}} if op != "commit" else {}
            required = []
            if op != "read":
                fields["intent_id"] = {"type": "string"}
                required.append("intent_id")
            if op == "commit":
                fields["prepare_digest"] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
                required.append("prepare_digest")
                fields["resume"] = {"type": "object", "properties": {
                    "approval_id": {"type": "string"}, "params": {"type": "object"}},
                    "required": ["approval_id", "params"], "additionalProperties": False}
            if name in tools:
                raise MachineError("Ambiguous capability catalog")
            schema = {"type": "object", "properties": fields, "required": required, "additionalProperties": False}
            tools[name] = ({"name": name, "description": description + (" (confirmation required)" if kind=="write" else ""),
                            "inputSchema": schema}, cap, op)
    return tools


def dispatch(client, method, params):
    if method == "initialize":
        return {"protocolVersion": VERSION, "capabilities": {"tools": {}}, "serverInfo": INFO}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [v[0] for v in catalog(client).values()]}
    if method != "tools/call":
        raise LookupError
    tool, cap, op = catalog(client)[params["name"]]
    args = params.get("arguments", {})
    schema = tool["inputSchema"]
    if type(args) != dict or set(args)-schema["properties"].keys() or not set(schema["required"]) <= args.keys():
        raise ValueError
    if op == "commit":
        if "resume" in args:
            resume = args["resume"]
            if not isinstance(resume, dict) or set(resume) != {"approval_id", "params"}:
                raise ValueError
            result = client.resume_pending(cap, args["intent_id"], args["prepare_digest"],
                                           resume["approval_id"], resume["params"])
        else:
            result = client.commit(cap, args["intent_id"], args["prepare_digest"])
    elif op == "prepare":
        result = client.prepare(cap, args["intent_id"], **args.get("params", {}))
    else:
        result = client.read(cap, **args.get("params", {}))
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def serve(client, source, sink):
    for line in source:
        ident = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError
            if "id" not in message:
                continue
            ident = message["id"]
            result = dispatch(client, message["method"], message.get("params", {}))
            response = {"result": result}
        except MachineError as exc:
            error = {"code": -32000, "message": str(exc)}
            if exc.retry_after is not None:
                error["data"] = {"retry_after_seconds": exc.retry_after}
            response = {"error": error}
        except json.JSONDecodeError:
            response = {"error": {"code": -32700, "message": "Parse error"}}
        except KeyError:
            response = {"error": {"code": -32602, "message": "Invalid tool or parameters"}}
        except LookupError:
            response = {"error": {"code": -32601, "message": "Method not found"}}
        except (TypeError, ValueError):
            response = {"error": {"code": -32602, "message": "Invalid method, tool or parameters"}}
        print(json.dumps({"jsonrpc":"2.0","id":ident,**response}), file=sink, flush=True)


def main():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, "Invalid command line; see --help\n")

    parser = Parser(description="Local NexLoop machine tools")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ca-file")
    args = parser.parse_args()
    try:
        client = Client(args.base_url, os.environ.get("NEXLOOP_MACHINE_KEY_FILE"), args.ca_file)
    except (MachineError, TypeError):
        parser.exit(2, "Invalid machine client configuration\n")
    serve(client, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
