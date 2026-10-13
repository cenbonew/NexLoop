-- NX-037 provider routing (docs/implementation/NX-037-provider-routing.md §6): an effect intent whose
-- Action has no configured provider route is never dispatched (the effect worker answers 'unrouted'). It is recorded like any
-- other effect refusal (authz.nexloop_record_effect_refusal, 0131: the executor's own intents only, one row per intent and code
-- within ten minutes), so it shows in the M12 metrics (refusals.last_5m by code) and the NX-030 alert rules
-- (deploy/configuration/alert-rules.v4.json: effect_route_unconfigured, warning).
insert into control.nexloop_dispatch_refusal_codes values ('NXE01','effect_route_unconfigured');
