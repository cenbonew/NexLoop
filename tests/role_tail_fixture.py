"""Main catalog owns the append; no manual DRAFT migration execution."""
import pytest
from role_run_fixture import role_runtime_plan
from runtime_effect_fixture import runtime_effect_plan
@pytest.fixture
def role_tail_plan(request):
    return request.getfixturevalue('role_runtime_plan')
