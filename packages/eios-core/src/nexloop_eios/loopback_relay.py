"""NX-037 D4 (owner ruling 2026-10-11) as deployed by NX-032: the Agent Host runs in its own network namespace; the two links it needs
(API/Runtime Worker -> Host control, Host -> guard) keep their loopback endpoints on both sides and cross the namespaces through an
internal link network by this TCP relay. TLS is end to end between the original peers (the relay only copies bytes), so the
loopback certificates, their 127.0.0.1 SAN and the Host's own Host-header check stay as they are.

    python -m nexloop_eios.loopback_relay --rule 127.0.0.1:8100=<agent link address>:8100 [--rule ...]

Each rule listens on LISTEN_HOST:PORT and connects every accepted connection to TARGET_HOST:PORT. Standard library only.
"""
import argparse
import asyncio
import ipaddress
import sys


def parse_rule(text):
    listen, sep, target = text.partition('=')
    if not sep:
        raise ValueError('rule LISTEN_HOST:PORT=TARGET_HOST:PORT')
    def endpoint(value):
        host, _, port = value.rpartition(':')
        ipaddress.ip_address(host.strip('[]'))
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError('port')
        return host.strip('[]'), port
    return endpoint(listen), endpoint(target)


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve(rules, ready=None):
    servers = []
    for (lhost, lport), (thost, tport) in rules:
        async def handle(reader, writer, thost=thost, tport=tport):
            try:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(thost, tport), timeout=5)
            except (OSError, asyncio.TimeoutError):
                writer.close()
                return
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        servers.append(await asyncio.start_server(handle, lhost, lport))
    if ready is not None:
        ready.set()
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--rule', action='append', required=True)
    args = parser.parse_args(argv)
    try:
        rules = [parse_rule(r) for r in args.rule]
    except ValueError:
        parser.exit(2, 'relay rule invalid\n')
    asyncio.run(serve(rules))


if __name__ == '__main__':
    sys.exit(main())
