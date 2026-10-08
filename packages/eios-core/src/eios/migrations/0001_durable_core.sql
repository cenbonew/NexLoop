create schema if not exists control;
create schema if not exists runtime;
create schema if not exists ontology;

create table if not exists control.schema_migrations (
  version text primary key,
  checksum text not null,
  applied_at timestamptz not null default now()
);

create table runtime.invocations (
  invocation_id text primary key,
  tenant_id text not null check (tenant_id <> ''),
  capability_name text not null,
  capability_version text not null,
  actor_id text not null,
  trace_id text not null,
  request_id text not null,
  correlation_id text not null,
  causation_id text not null default '',
  idempotency_key text,
  input_digest text not null,
  status text not null,
  job_id text not null default '',
  result jsonb,
  artifact_refs jsonb not null default '[]'::jsonb,
  error jsonb,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  unique (tenant_id, invocation_id)
);
create unique index runtime_invocation_idempotency
  on runtime.invocations (tenant_id, capability_name, capability_version, idempotency_key)
  where idempotency_key is not null and idempotency_key <> '';

create table runtime.jobs (
  job_id text primary key,
  invocation_id text not null,
  capability_name text not null,
  capability_version text not null,
  capability_type text not null,
  execution_mode text not null,
  tenant_id text not null check (tenant_id <> ''),
  actor_id text not null,
  trace_id text not null,
  request_id text not null,
  idempotency_key text,
  normalized_input jsonb not null,
  plan_snapshot jsonb not null,
  status text not null,
  result jsonb,
  artifact_refs jsonb not null default '[]'::jsonb,
  error jsonb,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  unique (tenant_id, job_id),
  foreign key (tenant_id, invocation_id)
    references runtime.invocations(tenant_id, invocation_id)
);
create unique index runtime_job_idempotency
  on runtime.jobs (tenant_id, capability_name, capability_version, idempotency_key)
  where idempotency_key is not null and idempotency_key <> '';

create table runtime.job_events (
  id bigint generated always as identity primary key,
  tenant_id text not null check (tenant_id <> ''),
  job_id text not null,
  sequence integer not null,
  status text not null,
  message text not null,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null,
  unique (job_id, sequence),
  foreign key (tenant_id, job_id)
    references runtime.jobs(tenant_id, job_id)
);

create table runtime.audit_events (
  event_id text primary key,
  action text not null,
  tenant_id text not null check (tenant_id <> ''),
  actor_id text not null,
  trace_id text not null,
  request_id text not null,
  capability_name text not null default '',
  invocation_id text not null default '',
  job_id text not null default '',
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null
);

create table runtime.artifact_references (
  artifact_id text primary key,
  tenant_id text not null check (tenant_id <> ''),
  capability_name text not null,
  content_type text not null,
  size_bytes bigint not null check (size_bytes >= 0),
  checksum text not null,
  storage_backend text not null,
  storage_class text not null default 'STANDARD',
  bucket text not null default '',
  object_key text not null default '',
  etag text not null default '',
  crc64 text not null default '',
  storage_uri text not null,
  status text not null default 'available',
  retention_status text not null default 'active',
  created_at timestamptz not null default now()
);

create table runtime.idempotency_records (
  tenant_id text not null,
  operation text not null,
  idempotency_key text not null,
  request_digest text not null,
  result_type text not null,
  result jsonb not null,
  created_at timestamptz not null default now(),
  primary key (tenant_id, operation, idempotency_key)
);

create table ontology.object_type_versions (
  tenant_id text not null,
  type_name text not null,
  version integer not null,
  definition jsonb not null,
  created_at timestamptz not null default now(),
  primary key (tenant_id, type_name, version)
);
create table ontology.relation_type_versions (
  tenant_id text not null,
  relation_name text not null,
  version integer not null,
  definition jsonb not null,
  created_at timestamptz not null default now(),
  primary key (tenant_id, relation_name, version)
);
create table ontology.event_type_versions (
  tenant_id text not null,
  event_name text not null,
  version integer not null,
  definition jsonb not null,
  created_at timestamptz not null default now(),
  primary key (tenant_id, event_name, version)
);

create table ontology.objects (
  tenant_id text not null,
  type_name text not null,
  object_id text not null,
  schema_version integer not null,
  properties jsonb not null,
  source_system text not null default '',
  source_ref text not null default '',
  created_at timestamptz not null,
  updated_at timestamptz not null,
  primary key (tenant_id, type_name, object_id)
);

create table ontology.relations (
  relation_id text primary key,
  tenant_id text not null,
  relation_name text not null,
  schema_version integer not null,
  source_type text not null,
  source_object_id text not null,
  target_type text not null,
  target_object_id text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null,
  foreign key (tenant_id, source_type, source_object_id)
    references ontology.objects(tenant_id, type_name, object_id),
  foreign key (tenant_id, target_type, target_object_id)
    references ontology.objects(tenant_id, type_name, object_id)
);

create table ontology.events (
  event_id text primary key,
  tenant_id text not null,
  event_type text not null,
  object_type text not null,
  object_id text not null,
  payload jsonb not null default '{}'::jsonb,
  actor_id text not null default '',
  sequence integer not null,
  created_at timestamptz not null,
  foreign key (tenant_id, object_type, object_id)
    references ontology.objects(tenant_id, type_name, object_id),
  unique (tenant_id, object_type, object_id, sequence)
);

create index runtime_invocations_tenant_created on runtime.invocations(tenant_id, created_at desc);
create index runtime_jobs_tenant_created on runtime.jobs(tenant_id, created_at desc);
create index runtime_jobs_tenant_invocation on runtime.jobs(tenant_id, invocation_id);
create index runtime_job_events_job_sequence on runtime.job_events(job_id, sequence);
create index runtime_audit_tenant_created on runtime.audit_events(tenant_id, created_at desc);
create index runtime_artifacts_tenant_created on runtime.artifact_references(tenant_id, created_at desc);
create index ontology_relations_source on ontology.relations(tenant_id, source_type, source_object_id);
create index ontology_relations_target on ontology.relations(tenant_id, target_type, target_object_id);
create index ontology_events_object_sequence on ontology.events(tenant_id, object_type, object_id, sequence);

do $$
declare
  target regclass;
begin
  foreach target in array array[
    'runtime.invocations'::regclass,
    'runtime.jobs'::regclass,
    'runtime.job_events'::regclass,
    'runtime.audit_events'::regclass,
    'runtime.artifact_references'::regclass,
    'runtime.idempotency_records'::regclass,
    'ontology.object_type_versions'::regclass,
    'ontology.relation_type_versions'::regclass,
    'ontology.event_type_versions'::regclass,
    'ontology.objects'::regclass,
    'ontology.relations'::regclass,
    'ontology.events'::regclass
  ] loop
    execute format('alter table %s enable row level security', target);
    execute format(
      'create policy tenant_isolation on %s using '
      || '(tenant_id = current_setting(''eios.tenant_id'', true)) with check '
      || '(tenant_id = current_setting(''eios.tenant_id'', true))',
      target
    );
    if exists (select 1 from pg_roles where rolname = 'nex_eios_runtime') then
      execute format(
        'create policy runtime_cross_tenant on %s to nex_eios_runtime using (true) with check (true)',
        target
      );
    end if;
  end loop;
end $$;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'nex_eios_app') then
    grant usage on schema control, runtime, ontology to nex_eios_app;
    grant select on control.schema_migrations to nex_eios_app;
    grant select, insert, update on runtime.invocations, runtime.jobs, runtime.artifact_references to nex_eios_app;
    grant select, insert on runtime.job_events, runtime.audit_events, runtime.idempotency_records to nex_eios_app;
    grant select, insert, update on ontology.objects to nex_eios_app;
    grant select, insert on ontology.object_type_versions, ontology.relation_type_versions,
      ontology.event_type_versions, ontology.relations, ontology.events to nex_eios_app;
    grant usage, select on all sequences in schema runtime to nex_eios_app;
  end if;
  if exists (select 1 from pg_roles where rolname = 'nex_eios_runtime') then
    grant usage on schema control, runtime, ontology to nex_eios_runtime;
    grant select on control.schema_migrations to nex_eios_runtime;
    grant select, insert, update on runtime.invocations, runtime.jobs, runtime.artifact_references to nex_eios_runtime;
    grant select, insert on runtime.job_events, runtime.audit_events, runtime.idempotency_records to nex_eios_runtime;
    grant usage, select on all sequences in schema runtime to nex_eios_runtime;
  end if;
end $$;
