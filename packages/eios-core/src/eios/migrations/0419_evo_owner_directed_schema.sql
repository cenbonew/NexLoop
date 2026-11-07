-- Owner-directed Schema proposals (owner 2026-11-07, option A; docs/implementation/schema-only-candidate-template.md, L6 PASS v2):
-- a property the owner decided on directly (this time Consumer.delivery_windows, claim-consumer-plan §10-2) may skip "the candidate
-- comes from an extraction or retrieval failure"; human review (ontology.schema.review) and the release permit (an accepted
-- nx040-gates-1 report) are unchanged. Its candidate carries its own category, so it never reads as a retrieval_miss or an
-- ambiguity_unresolved signal, and it can only be an ontology_schema candidate. Constraints only: no function is redefined; every
-- existing row satisfies both (they are retrieval_miss / ambiguity_unresolved). Temporary number; the dispatcher renumbers.
alter table runtime.nexloop_evo_candidates drop constraint nexloop_evo_candidates_category_check;
alter table runtime.nexloop_evo_candidates add constraint nexloop_evo_candidates_category_check
 check(category in ('retrieval_miss','ambiguity_unresolved','owner_directed_schema'));
alter table runtime.nexloop_evo_candidates add constraint nexloop_evo_candidates_owner_directed_dimension
 check(category<>'owner_directed_schema' or dimension='ontology_schema');
