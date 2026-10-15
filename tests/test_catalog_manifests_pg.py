"""NX-032 A2: the governed capabilities the validator reads from the migration catalog are exactly the rows of
control.nexloop_governed_capabilities in a bootstrapped clean catalog database."""
from nexloop_eios import catalog_manifests as C
from nexloop_eios.bootstrap import bootstrap


def test_the_static_governed_capabilities_equal_the_bootstrapped_table(admin):
    bootstrap(admin)
    rows = dict(admin.execute('select capability, operation from control.nexloop_governed_capabilities').fetchall())
    assert rows == C.governed_capabilities()
