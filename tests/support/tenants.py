"""NX-052: the synthetic test tenants, as uuid strings (control.nexloop_tenants accepts only canonical lowercase uuids).

Every test, fixture and load template names a tenant through these constants instead of a literal. They identify
synthetic tenants inside disposable clusters only; none of them is a real tenant."""

TENANT_A = '00000000-0000-4000-8000-00000000000a'  # formerly 'synthetic-a'
TENANT_B = '00000000-0000-4000-8000-00000000000b'  # formerly 'synthetic-b'
TENANT_OTHER = '00000000-0000-4000-8000-00000000000c'  # formerly 'synthetic-other' (a foreign tenant in isolation tests)
from nexloop_eios.sandbox_profile import SANDBOX_TENANT  # noqa: E402,F401  formerly 'nexloop-sandbox'; browser_test_profile.TENANT is the same
