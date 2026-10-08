-- No implicit PUBLIC execution on future governed owner functions.
-- Every application entry point must explicitly grant a narrow signature.
alter default privileges for role nexloop_owner revoke execute on functions from public;
alter default privileges for role nexloop_owner revoke all on tables from public;
alter default privileges for role nexloop_owner revoke all on sequences from public;
-- The runner/bootstrap account also cannot accidentally publish new definers.
alter default privileges revoke execute on functions from public;
-- Do not reuse PostgreSQL's permissive built-in object-creation defaults.
revoke create on schema control, runtime, ontology from public;
