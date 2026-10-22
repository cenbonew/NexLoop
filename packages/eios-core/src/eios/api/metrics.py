"""Process-local API metrics with Prometheus text exposition.

Queue/outbox/worker gauges intentionally stay with their owning processes
(the scheduler emits per-cycle JSON diagnostics; the database host runs the
read-only health probe): the API must not hold the runtime DSN, so it only
speaks for itself here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Final


_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.025,
    0.1,
    0.25,
    0.5,
    1.0,
    5.0,
)

_MAX_TRACKED_SERIES: Final[int] = 512


def _escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


@dataclass
class _Series:
    count: int = 0
    duration_sum: float = 0.0
    bucket_counts: list[int] = field(
        default_factory=lambda: [0] * (len(_DURATION_BUCKETS) + 1)
    )


class ApiMetricsRegistry:
    """Bounded, lock-protected request metrics keyed by route template."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: dict[tuple[str, str, int], _Series] = {}
        self._dropped = 0

    def observe(
        self,
        *,
        method: str,
        route: str,
        status: int,
        duration_seconds: float,
    ) -> None:
        key = (str(method), str(route), int(status))
        duration = max(0.0, float(duration_seconds))
        with self._lock:
            series = self._series.get(key)
            if series is None:
                if len(self._series) >= _MAX_TRACKED_SERIES:
                    self._dropped += 1
                    return
                series = _Series()
                self._series[key] = series
            series.count += 1
            series.duration_sum += duration
            for index, upper in enumerate(_DURATION_BUCKETS):
                if duration <= upper:
                    series.bucket_counts[index] += 1
                    break
            else:
                series.bucket_counts[-1] += 1

    def render(self, *, storage_mode: str, revision: str) -> str:
        lines = [
            "# HELP nexloop_build_info Deployment identity of this API process.",
            "# TYPE nexloop_build_info gauge",
            (
                "nexloop_build_info{"
                f'storage_mode="{_escape_label(storage_mode)}",'
                f'migration_revision="{_escape_label(revision)}"'
                "} 1"
            ),
            "# HELP nexloop_api_requests_total API requests by route and status.",
            "# TYPE nexloop_api_requests_total counter",
        ]
        with self._lock:
            items = sorted(self._series.items())
            dropped = self._dropped
            for (method, route, status), series in items:
                labels = (
                    f'method="{_escape_label(method)}",'
                    f'route="{_escape_label(route)}",'
                    f'status="{status}"'
                )
                lines.append(
                    f"nexloop_api_requests_total{{{labels}}} {series.count}"
                )
            lines.append(
                "# HELP nexloop_api_request_duration_seconds "
                "API request duration by route."
            )
            lines.append(
                "# TYPE nexloop_api_request_duration_seconds histogram"
            )
            for (method, route, status), series in items:
                base = (
                    f'method="{_escape_label(method)}",'
                    f'route="{_escape_label(route)}",'
                    f'status="{status}"'
                )
                cumulative = 0
                for index, upper in enumerate(_DURATION_BUCKETS):
                    cumulative += series.bucket_counts[index]
                    lines.append(
                        "nexloop_api_request_duration_seconds_bucket{"
                        f'{base},le="{upper}"'
                        f"}} {cumulative}"
                    )
                cumulative += series.bucket_counts[-1]
                lines.append(
                    "nexloop_api_request_duration_seconds_bucket{"
                    f'{base},le="+Inf"'
                    f"}} {cumulative}"
                )
                lines.append(
                    "nexloop_api_request_duration_seconds_sum{"
                    f"{base}}} {series.duration_sum:.6f}"
                )
                lines.append(
                    "nexloop_api_request_duration_seconds_count{"
                    f"{base}}} {series.count}"
                )
        lines.append(
            "# HELP nexloop_api_metric_series_dropped_total "
            "Series discarded after the bounded cardinality limit."
        )
        lines.append("# TYPE nexloop_api_metric_series_dropped_total counter")
        lines.append(f"nexloop_api_metric_series_dropped_total {dropped}")
        return "\n".join(lines) + "\n"


__all__ = ["ApiMetricsRegistry"]
