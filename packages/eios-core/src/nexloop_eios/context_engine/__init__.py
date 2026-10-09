"""NX-023 Context Engine (part A): strategies, budget, section partition, read-only inputs.

Pure assembly logic plus read-only, currently-authorized PostgreSQL inputs. Nothing in
this package writes business state, binds a Run, or grants authority (docs/05, ADR-019,
ADR-020). Run binding and per-request manifests arrive with NX-023-B.
"""
