"""OPERATIONS §8a.10 (10/16 on the stage: agent-host recreated, agent-link-relay left in the removed container's network namespace):
a service another service joins with `network_mode: service:<X>` is never recreated or restarted alone. Every Compose command in
the run sheets and OPERATIONS that names X (up / restart, explicit service list) also names every service that joins X."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / 'deploy/stage/compose.app.example.yaml'
DOCS = sorted((ROOT / 'docs/release').glob('*.md')) + [ROOT / 'docs/OPERATIONS.md']


def dependents():
    services = yaml.safe_load(COMPOSE.read_text())['services']
    graph = {}
    for name, spec in services.items():
        mode = str(spec.get('network_mode', ''))
        if mode.startswith('service:'):
            graph.setdefault(mode.split(':', 1)[1], set()).add(name)
    return graph, set(services)


def compose_commands(text):
    """Compose command lines (continuations joined) that recreate or restart an explicit list of services."""
    joined = re.sub(r'\\\n\s*', ' ', text)
    for line in joined.split('\n'):
        if 'docker compose' in line and re.search(r'\s(up|restart)\s', line):
            yield line


def named_services(command, known):
    tail = re.split(r'\s(?:up|restart)\s', command, maxsplit=1)[1]
    return {token for token in re.split(r'[\s`|;&]+', tail) if token in known}


def test_the_dependency_graph_is_the_one_operations_documents():
    graph, _ = dependents()
    assert graph['agent-host'] == {'agent-link-relay'} and graph['action-worker'] == {'webchat-delivery'} and len(graph['app-net']) == 18  # recall-indexer left app-net for claim_egress (2026-10-18); + outreach-sender (outreach slice 5)
    assert set(graph) == {'app-net', 'agent-host', 'action-worker'}
    text = (ROOT / 'docs/OPERATIONS.md').read_text()
    assert '8a.10' in text and all(name in text for names in graph.values() for name in names)


def test_every_documented_rebuild_carries_the_services_that_share_the_namespace():
    graph, known = dependents()
    problems = []
    for path in DOCS:
        for command in compose_commands(path.read_text()):
            named = named_services(command, known)
            for provider, joiners in graph.items():
                if provider in named and not joiners <= named:
                    problems.append((path.name, provider, sorted(joiners - named), command.strip()[:160]))
    assert not problems, problems


def test_the_check_catches_a_rebuild_that_leaves_a_joiner_behind():
    graph, known = dependents()
    text = 'docker compose -p nexloop-app -f c.yaml \\\n  --env-file e up -d --force-recreate agent-host claim-matcher\n'
    (command,) = list(compose_commands(text))
    named = named_services(command, known)
    assert 'agent-host' in named and not graph['agent-host'] <= named
