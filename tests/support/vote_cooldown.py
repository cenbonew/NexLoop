"""NX-058 slice D §2: NX-056 counts a vote only 24 hours after the voter's current holding of an approver role began (the role history,
control.nexloop_workbench_role_history). Tests whose synthetic members vote at once record that holding as begun `hours` ago: a start
row dated back for every active member of the tenant's latest workbench configuration (the history orders by recorded_at)."""


def ripen_approvers(admin, tenant, hours=25):
    admin.execute("""insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source,recorded_at)
        select %s,bm.subject_id,c.key,c.value,'start','synthetic-ripened',clock_timestamp()-make_interval(hours=>%s)
        from jsonb_each_text(coalesce((select w.members from control.nexloop_workbench_configurations w where w.tenant_id=%s
                                       order by w.manifest_version desc limit 1),'{}')) c
        join control.nexloop_browser_memberships bm on bm.tenant_id=%s and bm.principal_id=c.key and bm.payload->>'status'='active'""",
                  (tenant, hours, tenant, tenant))
