"""`nexloop-consumer`: the consumer channel client as a command line (phase 1; docs/implementation/consumer-channel-cli-mcp.md).

    nexloop-consumer [--origin URL] [--ca-file F] [--session-file F] login --username U (--password-stdin | --password-file F)
    nexloop-consumer [...] whoami | logout
    nexloop-consumer [...] new-conversation
    nexloop-consumer [...] conversations [--limit N]
    nexloop-consumer [...] send [--conversation-id ID] (--text T | --text-stdin)
    nexloop-consumer [...] wait-reply [--conversation-id ID] [--after-sequence N] [--timeout S] [--poll S]
    nexloop-consumer [...] history [--conversation-id ID] [--after N] [--limit N]
    nexloop-consumer [...] receipts --message-id ID
    nexloop-consumer [...] watch [--conversation-id ID] [--after N]

Every command prints exactly one JSON object on stdout (`ok` true or false with a `code`); exit 0 on ok, 1 on a refused or
failed call, 2 on bad arguments. The password never appears in argv: --password-stdin reads one line (for example from
`security find-generic-password -w -s <item> | nexloop-consumer … login --username U --password-stdin`), --password-file reads a
private file. The session (cookie and CSRF token) lives in the private --session-file (default ~/.nexloop/consumer/session.json,
0600) between commands and is never printed. --origin defaults to the session file's origin after the first login.
"""
import argparse
import json
import sys
from pathlib import Path

from nexloop_eios.consumer_channel import ConsumerChannelError, ConsumerClient, Session, load_session, private_session_path, \
    read_password, save_session

DEFAULT_SESSION = Path.home() / '.nexloop' / 'consumer' / 'session.json'


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({'ok': False, 'code': 'arguments'}))
        self.exit(2)


def parser():
    p = _Parser(prog='nexloop-consumer', description='NexLoop consumer channel (WebChat HTTP API) client')
    p.add_argument('--origin')
    p.add_argument('--ca-file', type=Path)
    p.add_argument('--session-file', type=Path, default=DEFAULT_SESSION)
    sub = p.add_subparsers(dest='command', required=True)
    login = sub.add_parser('login')
    login.add_argument('--username', required=True)
    source = login.add_mutually_exclusive_group(required=True)
    source.add_argument('--password-stdin', action='store_true')
    source.add_argument('--password-file', type=Path)
    sub.add_parser('whoami')
    sub.add_parser('logout')
    sub.add_parser('new-conversation')
    conversations = sub.add_parser('conversations')
    conversations.add_argument('--limit', type=int, default=50)
    send = sub.add_parser('send')
    send.add_argument('--conversation-id')
    text = send.add_mutually_exclusive_group(required=True)
    text.add_argument('--text')
    text.add_argument('--text-stdin', action='store_true')
    wait = sub.add_parser('wait-reply')
    wait.add_argument('--conversation-id')
    wait.add_argument('--after-sequence', type=int, default=0)
    wait.add_argument('--timeout', type=float, default=120.0)
    wait.add_argument('--poll', type=float, default=2.0)
    history = sub.add_parser('history')
    history.add_argument('--conversation-id')
    history.add_argument('--after', type=int, default=0)
    history.add_argument('--limit', type=int, default=50)
    receipts = sub.add_parser('receipts')
    receipts.add_argument('--message-id', required=True)
    watch = sub.add_parser('watch')
    watch.add_argument('--conversation-id')
    watch.add_argument('--after', type=int, default=0)
    return p


def run(args, *, stdin=sys.stdin, transport=None):
    path = private_session_path(args.session_file)
    session = load_session(path) if path.exists() else None
    origin = args.origin or (session.origin if session else None)
    if not origin:
        raise ConsumerChannelError('origin_required')
    if session is not None and session.origin != origin.rstrip('/'):
        session = None  # a different service: start a new session
    client = ConsumerClient(origin, transport=transport, ca_file=args.ca_file, session=session)
    command = args.command
    try:
        if command == 'login':
            password = read_password(password_file=args.password_file, stdin=stdin if args.password_stdin else None)
            client.session = Session(origin=client.origin)
            return client.login(args.username, password)
        if not client.session.cookie:
            raise ConsumerChannelError('login_required')
        if command == 'whoami':
            return client.whoami()
        if command == 'logout':
            return client.logout()
        if command == 'new-conversation':
            return client.create_conversation()
        if command == 'conversations':
            return client.list_conversations(limit=args.limit)
        if command == 'send':
            body = stdin.read() if args.text_stdin else args.text
            return client.send(body, conversation_id=args.conversation_id)
        if command == 'wait-reply':
            return client.wait_reply(args.conversation_id, after_sequence=args.after_sequence, timeout=args.timeout, poll=args.poll)
        if command == 'history':
            return client.history(args.conversation_id, after=args.after, limit=args.limit)
        if command == 'receipts':
            return client.receipt(args.message_id)
        if command == 'watch':
            return client.watch(args.conversation_id, after=args.after)
        raise ConsumerChannelError('command_unknown')
    finally:
        save_session(path, client.session)


def main(argv=None, *, stdin=sys.stdin, stdout=sys.stdout, transport=None):
    args = parser().parse_args(argv)
    try:
        result = run(args, stdin=stdin, transport=transport)
        code = 0
    except ConsumerChannelError as error:
        result, code = error.as_dict(), 1
    except (OSError, ValueError) as error:
        result, code = {'ok': False, 'code': type(error).__name__}, 1
    stdout.write(json.dumps(result, ensure_ascii=False) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
