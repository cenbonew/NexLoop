"""R76 step 5: publish the machine rate policy / note limits on a host that holds only the bootstrap DSN.

    machine_limits_publish.py publish-rate-policy --policy deploy/configuration/machine-rate-limits.v<N>.json --bootstrap-dsn-file F
    machine_limits_publish.py publish-note-limits --policy deploy/configuration/machine-note-limits.v<N>.json --bootstrap-dsn-file F

A thin wrapper (alert_rules_apply.py pattern): nexloop_bootstrap over the local socket opens the configurator window for this call
only, nexloop_eios.machine_access publishes through it (an unchanged file writes nothing) and the window closes. With a configurator DSN
file use `python -m nexloop_eios.machine_access` directly; the package does not depend on the deploy tools (production wheel).
Prints one JSON object; exit 0 published or unchanged, 1 rejected or unavailable, 2 usage."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packages/eios-core/src'))

from configurator_window import configurator_window  # noqa: E402
from nexloop_eios import machine_access as M  # noqa: E402

PUBLISH = {'publish-rate-policy': M.publish_rate_policy, 'publish-note-limits': M.publish_note_limits}


def main(argv=None):
    p = argparse.ArgumentParser(prog='machine_limits_publish.py')
    p.add_argument('command', choices=sorted(PUBLISH))
    p.add_argument('--policy', type=Path, required=True)
    p.add_argument('--bootstrap-dsn-file', type=Path, required=True)
    a = p.parse_args(argv)
    try:
        with configurator_window(a.bootstrap_dsn_file) as configurator:
            result = PUBLISH[a.command](a.policy, database_url_file=configurator)
    except (M.RatePolicyRejected, ValueError, OSError):
        print(json.dumps({'error': 'policy_rejected_or_unavailable'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
