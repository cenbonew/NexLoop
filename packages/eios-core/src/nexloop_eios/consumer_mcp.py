"""`nexloop-consumer-mcp`: the consumer channel client as an MCP server over stdio (phase 1; consumer-channel-cli-mcp.md).

    nexloop-consumer-mcp --origin https://<webchat origin> [--ca-file F] (--password-dir D | --keychain-service S)

Tools: login, new_conversation, send_message, wait_for_reply, get_history, get_receipt. The login tool takes a username only:
the password is read by the server from --password-dir/<username> (a private 0600 file) or from the macOS Keychain
(`security find-generic-password -w -s <service> -a <username>`, run without a shell), so it never enters the agent's
transcript, the tool arguments or any result. Results carry no cookie or CSRF token. The session lives in this process only.

Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio transport); `initialize`, `tools/list`, `tools/call`,
`ping`; notifications are accepted and ignored. Nothing is written to stdout except protocol messages.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from nexloop_eios.consumer_channel import ConsumerChannelError, ConsumerClient, read_password

PROTOCOL_VERSION = '2024-11-05'
_USERNAME = re.compile(r'[A-Za-z0-9._@+-]{1,128}')

TOOLS = [
    {'name': 'login', 'description': 'Sign in as a test consumer (password supplied by the server configuration, never by the caller).',
     'inputSchema': {'type': 'object', 'properties': {'username': {'type': 'string'}}, 'required': ['username'], 'additionalProperties': False}},
    {'name': 'new_conversation', 'description': 'Start a new conversation for the signed-in consumer.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'send_message', 'description': 'Send a consumer message (to the given conversation, else the current one, else a new one).',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 8192}, 'conversation_id': {'type': 'string'}},
                     'required': ['text'], 'additionalProperties': False}},
    {'name': 'wait_for_reply', 'description': 'Wait until a reply (a message not from this consumer) appears after a sequence number.',
     'inputSchema': {'type': 'object', 'properties': {'conversation_id': {'type': 'string'}, 'after_sequence': {'type': 'integer', 'minimum': 0},
                                                      'timeout_seconds': {'type': 'number', 'minimum': 1, 'maximum': 900}},
                     'additionalProperties': False}},
    {'name': 'get_history', 'description': 'Read the conversation messages after a sequence number.',
     'inputSchema': {'type': 'object', 'properties': {'conversation_id': {'type': 'string'}, 'after': {'type': 'integer', 'minimum': 0},
                                                      'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
                     'additionalProperties': False}},
    {'name': 'get_receipt', 'description': 'Read the service receipt and Run state of one of the consumer\'s messages.',
     'inputSchema': {'type': 'object', 'properties': {'message_id': {'type': 'string'}}, 'required': ['message_id'], 'additionalProperties': False}},
]


class PasswordSource:
    def __init__(self, *, password_dir=None, keychain_service=None, run=subprocess.run):
        if (password_dir is None) == (keychain_service is None):
            raise ValueError('exactly one password source')
        self.password_dir, self.keychain_service, self.run = password_dir, keychain_service, run

    def __call__(self, username):
        if not _USERNAME.fullmatch(username or ''):
            raise ConsumerChannelError('username_invalid')
        if self.password_dir is not None:
            return read_password(password_file=Path(self.password_dir) / username)
        done = self.run(['security', 'find-generic-password', '-w', '-s', self.keychain_service, '-a', username],
                        capture_output=True, text=True, timeout=10)
        value = (done.stdout or '').rstrip('\n')
        if done.returncode != 0 or not value:
            raise ConsumerChannelError('password_unavailable')
        return value


class Server:
    def __init__(self, client, passwords):
        self.client, self.passwords = client, passwords

    def call(self, name, arguments):
        c = self.client
        if name == 'login':
            return c.login(arguments['username'], self.passwords(arguments['username']))
        if not c.session.cookie:
            raise ConsumerChannelError('login_required')
        if name == 'new_conversation':
            return c.create_conversation()
        if name == 'send_message':
            return c.send(arguments['text'], conversation_id=arguments.get('conversation_id'))
        if name == 'wait_for_reply':
            return c.wait_reply(arguments.get('conversation_id'), after_sequence=arguments.get('after_sequence', 0),
                                timeout=min(float(arguments.get('timeout_seconds', 120)), 900.0))
        if name == 'get_history':
            return c.history(arguments.get('conversation_id'), after=arguments.get('after', 0), limit=arguments.get('limit', 50))
        if name == 'get_receipt':
            return c.receipt(arguments['message_id'])
        raise ConsumerChannelError('tool_unknown')

    def handle(self, message):
        """One JSON-RPC message → a response dict, or None for a notification."""
        if type(message) is not dict or message.get('jsonrpc') != '2.0' or type(message.get('method')) is not str:
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}}
        method, ident = message['method'], message.get('id')
        if ident is None:
            return None
        params = message.get('params') or {}
        if method == 'initialize':
            result = {'protocolVersion': params.get('protocolVersion') or PROTOCOL_VERSION, 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'nexloop-consumer', 'version': '1'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            name, arguments = params.get('name'), params.get('arguments') or {}
            try:
                if type(arguments) is not dict:
                    raise ConsumerChannelError('arguments_invalid')
                out, is_error = self.call(name, arguments), False
            except ConsumerChannelError as error:
                out, is_error = error.as_dict(), True
            except (KeyError, TypeError, ValueError):
                out, is_error = {'ok': False, 'code': 'arguments_invalid'}, True
            result = {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}], 'isError': is_error}
        else:
            return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'method not found'}}
        return {'jsonrpc': '2.0', 'id': ident, 'result': result}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout):
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
            else:
                response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
                stdout.flush()


def main(argv=None, *, transport=None, stdin=sys.stdin, stdout=sys.stdout, run=subprocess.run):
    p = argparse.ArgumentParser(prog='nexloop-consumer-mcp', description='NexLoop consumer channel MCP server (stdio)')
    p.add_argument('--origin', required=True)
    p.add_argument('--ca-file', type=Path)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--password-dir', type=Path)
    source.add_argument('--keychain-service')
    args = p.parse_args(argv)
    client = ConsumerClient(args.origin, transport=transport, ca_file=args.ca_file)
    Server(client, PasswordSource(password_dir=args.password_dir, keychain_service=args.keychain_service, run=run)).serve(stdin, stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
