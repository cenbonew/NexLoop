"""Explicit trusted activation of an already governed shared PlanStep.

The genuine Source-issued Run and Planner BIND must already exist. This entry
never inherits planner, queue or executor rights from a role mapping, and never
issues a new Run implicitly on a replay. Callers retain the existing trusted Run
vault; raw credentials are neither emitted nor put into Context/queue payload.
"""
from nexloop_eios.role_runs import bind_role_run
from nexloop_eios.role_context_artifacts import RoleContextArtifactProducer,RoleContextV6ArtifactProducer


def activate_role_plan(*,source,queue_service,run,command,body,consumer_id,role_id,link_id,step_id,offering_id,binding_id,control_id,queue='operations',context_strategy=None):
    if source._session.run_context is not None or queue_service._session.run_context is not None:
        raise PermissionError('trusted Source and queue service required')
    auth=source._session.authentication;keeper=queue_service._session.authentication
    if source._session.world!='real' or queue_service._session.world!='real' or auth.tenant_id!=keeper.tenant_id:
        raise PermissionError('role activation unavailable')
    # Actor/tenant/world are derived, never accepted as an alternative identity.
    if command['run_id']!=run.run_id or command['tenant_id']!=auth.tenant_id or command['world_id']!='real' or command['mode']!='real':
        raise PermissionError('role activation unavailable')
    selected=bind_role_run(source,run_id=run.run_id,consumer_id=consumer_id,role_id=role_id,link_id=link_id,step_id=step_id)
    command={**command,'role_ref':selected['role_ref'],'consumer_ref':'consumer:'+consumer_id,'credential_ref':'run:'+run.run_id}
    # NX-023: an explicit strategy selects Context v6 (frozen v3/v5 core + Engine sections).
    producer=RoleContextArtifactProducer(source) if context_strategy is None else RoleContextV6ArtifactProducer(source,context_strategy)
    pack=producer.prepare(run_token=run.token,command=command,body=body,offering_id=offering_id,binding_id=binding_id,control_id=control_id)
    command={**command,'context_manifest_ref':pack['artifact_ref']}
    result=queue_service.accept_runtime_event(queue=queue,source_id='nexloop-role-trigger',event_id='role-trigger:'+command['trigger_event_id'],run_token=run.token,command=command,input=pack['input'])
    return {'run_id':run.run_id,'command':command,'input':pack['input'],'admission':result,
            'context_attestation':{key:pack[key] for key in ('artifact_ref','sha256','command_binding_digest')}}
