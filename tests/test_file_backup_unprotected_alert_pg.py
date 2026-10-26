"""NX-035 D6, alert rules v20 file_backup_unprotected through the real alert evaluator (NX-030 fixture): an Artifact backupable for 2
hours and no file snapshot ever taken - the stage of NX-061 B9 - fires it while file_backup_age (absent metric) stays silent; inside the
hour it resolves. Synthetic Artifact rows injected by the test superuser (test_file_backup_pg's helper)."""
from pathlib import Path

from nexloop_eios.observability import apply_rules, load_rules
from test_file_backup_pg import artifact
from test_file_backup_unprotected_pg import became_backupable
from test_observability_pg import TENANT, observed  # noqa: F401
from test_workbench_read_pg import browser_business, conversations, identity, published_action, uow, workbench  # noqa: F401

RULES = Path(__file__).resolve().parents[1] / 'deploy/configuration'


def test_the_evaluator_fires_while_nothing_was_ever_backed_up_and_resolves_once_covered(observed):  # noqa: F811
    """The stage case of NX-061 B9: Artifacts on the stage, the file backup never enabled - file_backup_age stays silent, this fires."""
    o, admin = observed, observed['admin']
    assert apply_rules(load_rules(RULES / 'alert-rules.v20.json'), TENANT, database_url_file=o['w']['configurator'])['configured'] is True
    key, _ = artifact(admin, TENANT)
    became_backupable(admin, artifact_key=key, ago='2 hours')
    o['evaluate']()
    state = lambda rule: admin.execute("select firing,severity,last_value from control.nexloop_alert_state where rule_id=%s",  # noqa: E731
                                       (rule,)).fetchone()
    firing, severity, value = state('file_backup_unprotected')
    assert firing is True and severity == 'warning' and 7190 <= value <= 7260
    assert state('file_backup_age') in (None, (False, 'warning', None))  # absent metric: the old rule never fires here
    became_backupable(admin, artifact_key=key, ago='10 minutes')  # inside the hour: below the threshold
    o['evaluate']()
    assert state('file_backup_unprotected')[0] is False
