-- NX-040 F4, the last step (D21, owner 2026-10-20): the ontology_schema dimension opens for evaluation (NX-039 §12.1: the audited
-- open-dimension row). This migration does nothing else, so that the owner can decide its reversal on its own (a later migration
-- deleting the row closes the dimension again; evaluations then answer dimension_closed). The migration number lives only in the file
-- name and the catalog.
insert into control.nexloop_evo_open_dimensions(dimension,opened_by,decision)
 values('ontology_schema','migration:nx040-f4','NX-040 D21 (owner 2026-10-20): after G9-G13 (nx040-gates-1), the release permit and the watch window, ontology_schema candidates are evaluated; publication stays by hand (D22)');
