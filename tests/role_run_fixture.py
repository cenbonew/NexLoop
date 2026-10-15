"""Actual governed role selection added before generic real Run issuance.

No fake Source identity or runtime callback authority. Context remains explicitly
legacy until actual protected v3 Artifact bind is implemented and tested.
"""
from pathlib import Path
from datetime import datetime,UTC,timedelta
import pytest
from eios.authz.resources import ResourceType
from eios.authz.operations import Operation
from nexloop_eios.backend import AuthenticatedServices
from nexloop_eios.role_mapping import RoleMappingPort
from nexloop_eios.role_runs import bind_role_run,ROLE_FIELDS,LINK_FIELDS,STEP_FIELDS
from nexloop_eios.role_policies import CEILING_FIELDS,SCOPE_FIELDS
# Same bounded budget as runtime_effect_fixture Run commands; must stay within the ceiling.
# Per-Run distinct-submission ceiling; effect_units tests raise EffectControl budget instead.
ROLE_EFFECT_UNITS=1
ROLE_BUDGET={'maximum_model_turns':8,'maximum_tool_calls':8,'active_timeout_seconds':60,'maximum_cost':'1.0','currency':'USD'}
from nexloop_eios.service_offerings import OFFERING_FIELDS,BINDING_FIELDS
import runtime_effect_fixture as fixture
from runtime_effect_fixture import runtime_effect_plan
from system_catalog_fixture import publish_system_catalog

@pytest.fixture
def role_runtime_plan(monkeypatch,admin,request):
    original=fixture.install_runtime_catalog;issue=AuthenticatedServices.issue_run_credential;accept=AuthenticatedServices.accept_runtime_event;create_activation=AuthenticatedServices.create_runtime_activation
    roles={};policies={};policy_editors=[];bindings={};packs={};catalogs={};binding_sources={};current_sources=[];end_tokens=[]
    def install(admin,tenant,api,consumer,sources,expiry):
        tokens,sources=original(admin,tenant,api,consumer,sources,expiry)
        # NX-032 A1 T4: the role types and Actions as the production catalog builds them (system_catalog; role Actions bind the
        # 1.1.0 snapshots with ontology.roles.manage that 0057/0081 require), not hand-built definitions.
        from nexloop_eios.role_context_artifacts import ACTION as CONTEXT
        publish_system_catalog(admin,tenant,('RoleDefinition','ConsumerRoleLink','RoleExecutionCeiling','RoleAssignmentScope'),
            ('RoleDefinition.create','ConsumerRoleLink.create','RoleExecutionCeiling.create','RoleAssignmentScope.create',
             'ConsumerRoleLink.edit','RoleExecutionCeiling.edit','RoleAssignmentScope.edit',CONTEXT))
        targets=[('eios:action:'+name+'.create:1',ResourceType.ACTION,Operation.EXECUTE) for name in ('RoleDefinition','ConsumerRoleLink','RoleExecutionCeiling','RoleAssignmentScope')]
        keeper_token=fixture.seed_multi_uuid(admin,tenant,targets,suffix='-role-maintainer',extra_scopes=('ontology.roles.manage',))
        keeper=api.authenticate(keeper_token,world='real')
        roleport=RoleMappingPort(keeper);now=datetime.now(UTC);valid=dict(valid_from=(now-timedelta(seconds=1)).isoformat(),valid_until=expiry)
        step=admin.execute("select object_id from ontology.objects where tenant_id=%s and type_name='PlanStep'",(tenant,)).fetchone()[0]
        out=[];newtokens=[]
        for index,source in enumerate(sources):
            roleport=RoleMappingPort(api.authenticate(keeper_token,world='real'))
            principal=source._session.authentication.subject_principal_id
            from nexloop_eios.role_policies import RolePolicyPort
            goal=admin.execute("select properties->>'goal_id' from ontology.objects where tenant_id=%s and object_id=%s",(tenant,step)).fetchone()[0]
            ceiling=RolePolicyPort(api.authenticate(keeper_token,world='real')).create(kind='RoleExecutionCeiling',intent_id='role-policy-ceiling-'+str(index),properties=dict(active=True,
                action_resources=['eios:action:'+fixture.EFFECT+':1'],consumer_ids=[consumer],goal_ids=[goal],step_ids=[step],budget=dict(ROLE_BUDGET),effect_units=ROLE_EFFECT_UNITS,**valid))['object_id']
            roleport=RoleMappingPort(api.authenticate(keeper_token,world='real'))
            role=roleport.create_role(intent_id='runtime-role-'+str(index),name='source-role-'+str(index),responsibility='governed service responsibility',ceiling_ref=ceiling,**valid)['object_id']
            scope=RolePolicyPort(api.authenticate(keeper_token,world='real')).create(kind='RoleAssignmentScope',intent_id='role-policy-scope-'+str(index),properties=dict(active=True,
                role_id=role,consumer_id=consumer,goal_ids=[goal],step_ids=[step],**valid))['object_id']
            roleport=RoleMappingPort(api.authenticate(keeper_token,world='real'))
            link=roleport.assign(intent_id='runtime-map-'+str(index),consumer_id=consumer,role_id=role,scope=scope,**valid)['object_id']
            catalog=admin.execute("select object_id,properties from ontology.objects where tenant_id=%s and type_name='ConsumerServiceOffering' and properties->>'source_principal'=%s",(tenant,principal)).fetchone()
            targets=[('eios:action:'+fixture.EFFECT+':1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:'+CONTEXT+':1',ResourceType.ACTION,Operation.EXECUTE),('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.CREATE),('eios:artifact:local_real',ResourceType.ARTIFACT,Operation.READ)]
            from nexloop_eios.effect_contexts import effect_plan_schemas
            goal,control=admin.execute("select properties->>'goal_id',properties->>'control_id' from ontology.objects where tenant_id=%s and object_id=%s",(tenant,step)).fetchone()
            for kind,obj,fields in [('Consumer',consumer,()),('Goal',goal,()),('EffectControl',control,('allow_effect','budget_units','valid_until','executor_principal')),('ServiceOffering',catalog[1]['offering_id'],OFFERING_FIELDS),('ConsumerServiceOffering',catalog[0],BINDING_FIELDS),('RoleDefinition',role,ROLE_FIELDS),('ConsumerRoleLink',link,LINK_FIELDS),('PlanStep',step,STEP_FIELDS),('RoleExecutionCeiling',ceiling,CEILING_FIELDS),('RoleAssignmentScope',scope,SCOPE_FIELDS)]:
                targets.append(('eios:object:'+kind+'/'+obj,ResourceType.OBJECT,Operation.READ))
                targets.extend(('eios:property:'+kind+'/'+obj+'/'+f,ResourceType.PROPERTY,Operation.READ) for f in fields)
            admin.execute('delete from authz.nexloop_service_credentials where token_digest=%s',(source._session.token_digest,))
            token=fixture.seed_multi_uuid(admin,tenant,targets,suffix='-source-'+('A','B')[index]);current=api.authenticate(token,world='real')
            catalogs[principal]=dict(offering_id=catalog[1]['offering_id'],binding_id=catalog[0],control_id=control)
            roles[principal]=dict(consumer_id=consumer,link_id=link,role_id=role,step_id=step)
            policies[principal]=dict(goal_id=goal,ceiling_id=ceiling,scope_id=scope,budget=dict(ROLE_BUDGET))
            out.append(current);newtokens.append(token)
        end_targets=[('eios:action:ConsumerRoleLink.edit:1',ResourceType.ACTION,Operation.EXECUTE)]
        for selected in roles.values():
            end_targets.extend([('eios:object:ConsumerRoleLink/'+selected['link_id'],ResourceType.OBJECT,Operation.EDIT),('eios:property:ConsumerRoleLink/'+selected['link_id']+'/active',ResourceType.PROPERTY,Operation.EDIT)])
        end_tokens.append(fixture.seed_multi_uuid(admin,tenant,end_targets,suffix='-role-end-manager',extra_scopes=('ontology.roles.manage',)))
        edit_targets=[('eios:action:RoleExecutionCeiling.edit:1',ResourceType.ACTION,Operation.EXECUTE),('eios:action:RoleAssignmentScope.edit:1',ResourceType.ACTION,Operation.EXECUTE)]
        for selected in policies.values():
            for kind,obj,fields in (('RoleExecutionCeiling',selected['ceiling_id'],CEILING_FIELDS),('RoleAssignmentScope',selected['scope_id'],SCOPE_FIELDS)):
                edit_targets.append(('eios:object:'+kind+'/'+obj,ResourceType.OBJECT,Operation.EDIT))
                edit_targets.extend(('eios:property:'+kind+'/'+obj+'/'+f,ResourceType.PROPERTY,Operation.EDIT) for f in fields)
        policy_editors.append(fixture.seed_multi_uuid(admin,tenant,edit_targets,suffix='-role-policy-editor',extra_scopes=('ontology.roles.manage',)))
        current_sources.extend(api.authenticate(token,world="real") for token in newtokens)
        return newtokens,current_sources
    def issued(self,**kwargs):
        run=issue(self,**kwargs)
        selected=roles.get(self._session.authentication.subject_principal_id)
        if selected is not None:
            from nexloop_eios.role_policies import bind_policy_run
            bind_policy_run(self,role_parameters=dict(run_id=run.run_id,**selected),
                policy_parameters=dict(run_id=run.run_id,**selected,**policies[self._session.authentication.subject_principal_id]))
            bindings[run.run_id]=bind_role_run(self,run_id=run.run_id,**selected)
            binding_sources[run.run_id]=self._session.authentication.subject_principal_id
        return run
    def accepted(self,**kwargs):
        command=kwargs['command'];binding=bindings.get(command['run_id'])
        if binding is not None and kwargs['source_id']!='nexloop-role-trigger':
            from nexloop_eios.role_activation import activate_role_plan
            source=next(source for source in current_sources if source._session.authentication.subject_principal_id==binding_sources[command['run_id']])
            from nexloop_eios.run_credentials import RunCredential,AUDIENCE
            # Reconstruct no authority: actual issued Run token + ledger expiry,
            # authenticated again by the typed entry and owner SQL boundaries.
            credential=RunCredential(command['run_id'],AUDIENCE,__import__('datetime').datetime.fromisoformat(binding['expires_at']),kwargs['run_token'])
            activated=activate_role_plan(source=source,queue_service=self,run=credential,command=command,body=kwargs['input'],
                **roles[source._session.authentication.subject_principal_id],**catalogs[source._session.authentication.subject_principal_id])
            command.clear();command.update(activated['command'])
            packs[command['run_id']]={**activated['context_attestation'],'input':activated['input']}
            return activated['admission']
        return accept(self,**kwargs)
    def activate(self,**kwargs):
        pack=packs.get(kwargs['run_id'])
        if pack is not None:kwargs['input']=pack['input']
        return create_activation(self,**kwargs)
    monkeypatch.setattr(AuthenticatedServices,'create_runtime_activation',activate)
    monkeypatch.setattr(fixture,'install_runtime_catalog',install)
    monkeypatch.setattr(AuthenticatedServices,'issue_run_credential',issued)
    monkeypatch.setattr(AuthenticatedServices,'accept_runtime_event',accepted)
    plan=request.getfixturevalue('runtime_effect_plan');plan['role_bindings']=bindings;plan['context_packs']=packs
    def end_role(run_id):
        manager=plan['api'].authenticate(end_tokens[0],world='real')
        link=bindings[run_id]['link_id']
        return RoleMappingPort(manager).end(intent_id='end-runtime-map:'+run_id,link_id=link,expected_revision=1)
    plan['end_role']=end_role
    def edit_policy(index,kind,patch):
        principal=plan['sources'][index]._session.authentication.subject_principal_id
        selected=policies[principal];object_id=selected['ceiling_id' if kind=='RoleExecutionCeiling' else 'scope_id']
        current=admin.execute('select properties,nexloop_revision from ontology.objects where tenant_id=%s and object_id=%s',(plan['tenant'],object_id)).fetchone()
        editor=plan['api'].authenticate(policy_editors[0],world='real')
        from nexloop_eios.role_policies import RolePolicyPort
        return RolePolicyPort(editor).edit(kind=kind,intent_id='edit-'+kind+'-'+str(index)+'-'+'-'.join(sorted(patch)),object_id=object_id,expected_revision=current[1],properties={**current[0],**patch})
    plan['edit_policy']=edit_policy;plan['role_policies']=policies
    plan['role_selection']=roles;plan['role_catalogs']=catalogs  # NX-025: plan recipes name the same governed Role objects
    return plan
