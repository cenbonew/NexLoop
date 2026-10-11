"""NX-037: the synthetic driver (nexloop_eios.interaction_drivers) bound to the synthetic test tenants."""
from nexloop_eios.interaction_drivers import SyntheticCollector  # noqa: F401
from nexloop_eios.interaction_drivers import SyntheticDriver as _Driver
from support.tenants import TENANT_A, TENANT_OTHER


def SyntheticDriver(*, tenant=TENANT_A, inject=None):  # noqa: N802 - keeps the class-like call sites
    return _Driver(tenant=tenant, other_tenant=TENANT_OTHER, inject=inject)
