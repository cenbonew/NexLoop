"""Prometheus metrics endpoint: scoped access and bounded process-local series."""

from __future__ import annotations

from eios.api.metrics import ApiMetricsRegistry


def test_registry_bounds_cardinality_and_escapes_labels() -> None:
    registry = ApiMetricsRegistry()
    registry.observe(
        method="GET",
        route='/api/quo"te\n',
        status=200,
        duration_seconds=0.2,
    )
    for index in range(600):
        registry.observe(
            method="GET",
            route=f"/api/unbounded/{index}",
            status=200,
            duration_seconds=0.01,
        )
    body = registry.render(storage_mode="memory", revision="0007")
    dropped = int(body.rsplit(" ", 1)[-1])
    assert dropped >= 89
    assert '\\"te\\n' in body
