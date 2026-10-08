# Target contracts and licensing baseline

`packages/contracts/` contains the live v1 NexLoop JSON Schemas copied from the handoff. They are targets, not upstream EIOS/Pi APIs. Their required date-time patterns and negative test fixtures were checked by the handoff validator (six schemas and eleven negative cases). Business facts remain EIOS/PostgreSQL; Pi SQLite stores only short-run runtime state. Evo semantics/candidates are non-authoritative business views.

NexLoop uses MIT. The owner authorized NEX-EIOS publication; frozen upstream has no LICENSE/NOTICE. Selected owner-authored source receives NexLoop MIT attribution, original source hashes and file provenance. Third-party Pi/Evo MIT texts are preserved separately; their authorship is not replaced. Extraction must not bring original production data/configuration or git history. An eventual dependency SBOM/license scan belongs to NX-036; this file is the S0 license plan, not a completed supply-chain audit.

Version lock records three verified commits and the verified Durable npm/source mismatch. Source build is mandatory. All image digests remain empty with unresolved_not_deployable status; no release can deploy them as if pinned.
