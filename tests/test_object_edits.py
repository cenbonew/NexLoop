import hashlib
import secrets
from datetime import UTC, datetime

import pytest
import psycopg
from psycopg.types.json import Jsonb
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from eios.authz.applications import ResourceRestriction, OperationRestriction
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from authority_fixture import replace_fact
from multi_authority_fixture import seed_multi_authority
from test_action_definitions import published_action


@pytest.fixture
def editor(published_action, admin):
    reader, definition, capability = published_action
    receipt = GovernedObjectCreator(reader.pool, reader.session, reader.signer).create(
        action_name='Consumer.create', action_version=1, intent_id='synthetic-edit-create',
        type_name='Consumer', properties={'preference':'old'})
    binding = definition.capability_binding.model_copy(update={'capability_name':'consumer.edit'})
    definition = definition.model_copy(update={'stable_name':'Consumer.edit','capability_binding':binding})
    capability = capability.model_copy(update={'capability_name':'consumer.edit'})
    admin.execute('insert into control.nexloop_action_definitions(tenant_id,world,resource_id,definition,capability) values(%s,%s,%s,%s,%s)',
        ('synthetic-a','real','eios:action:Consumer.edit:1',Jsonb(definition.model_dump(mode='json')),Jsonb(capability.model_dump(mode='json'))))
    targets=[('eios:action:Consumer.edit:1',ResourceType.ACTION,Operation.EXECUTE),
        ('eios:object:Consumer/'+receipt['object_id'],ResourceType.OBJECT,Operation.EDIT),
        ('eios:property:Consumer/'+receipt['object_id']+'/preference',ResourceType.PROPERTY,Operation.EDIT)]
    session,token=seed_multi_authority(admin,reader.pool,targets,identity_suffix='-editor')
    return GovernedObjectEditor(reader.pool,session,reader.signer),receipt['object_id'],token


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_governed_preference_edit_commits_and_replays(editor,admin):
    service,obj,token=editor
    args=dict(action_name='Consumer.edit',action_version=1,intent_id='synthetic-edit-001',type_name='Consumer',object_id=obj,expected_revision=1,properties={'preference':'new'})
    receipt=service.edit(**args)
    assert receipt=={'object_id':obj,'type_name':'Consumer','world':'real','revision':2}
    assert service.edit(**args)==receipt
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'new'},2)
    args.update(intent_id='synthetic-edit-002',expected_revision=2,properties={'preference':'next'})
    assert service.edit(**args)['revision']==3
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'next'},3)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_stale_revision_does_not_mutate_object(editor,admin):
    service,obj,token=editor
    with pytest.raises(psycopg.errors.SerializationFailure):
        service.edit(action_name='Consumer.edit',action_version=1,intent_id='synthetic-stale',type_name='Consumer',object_id=obj,expected_revision=2,properties={'preference':'bad'})
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'old'},1)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_property_permission_revocation_denies_patch(editor,admin):
    service,obj,token=editor
    replace_fact(admin,'synthetic-a','grants',['synthetic-a-editor-principal','eios:property:Consumer/'+obj+'/preference'],F.GrantFacts,grants=[])
    # Fresh authenticated identity must still deny the revoked property grant.
    service.session=authenticate_service(service.pool,token,world='real')
    from eios.authz.errors import AuthorizationUnavailable
    with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):
        service.edit(action_name='Consumer.edit',action_version=1,intent_id='synthetic-revoked',type_name='Consumer',object_id=obj,expected_revision=1,properties={'preference':'bad'})
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'old'},1)

@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_conflicting_intent_does_not_reapply_patch(editor,admin):
    service,obj,token=editor
    args=dict(action_name='Consumer.edit',action_version=1,intent_id='synthetic-edit-conflict',type_name='Consumer',object_id=obj,expected_revision=1,properties={'preference':'new'})
    service.edit(**args)
    from eios.actions.governance import ActionGovernanceError
    with pytest.raises(ActionGovernanceError):service.edit(**(args|{'properties':{'preference':'bad'}}))
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'new'},2)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_wrong_object_target_denied(editor,admin):
    service,obj,token=editor
    with pytest.raises((AuthorizationUnavailable,ActionAuthorizationDenied)):
        service.edit(action_name='Consumer.edit',action_version=1,intent_id='synthetic-other-object',type_name='Consumer',object_id='f'*64,expected_revision=1,properties={'preference':'bad'})
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'old'},1)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_revocation_after_resolution_before_sql_is_denied(editor,admin,monkeypatch):
    import nexloop_eios.object_edits as edits
    service,obj,token=editor
    original=edits.canonical_payload
    def race(value):
        text=original(value)
        if value.get('protocol')=='nexloop-object-edit-v1':
            replace_fact(admin,'synthetic-a','grants',['synthetic-a-editor-principal','eios:property:Consumer/'+obj+'/preference'],F.GrantFacts,grants=[])
        return text
    monkeypatch.setattr(edits,'canonical_payload',race)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        service.edit(action_name='Consumer.edit',action_version=1,intent_id='synthetic-edit-race',type_name='Consumer',object_id=obj,expected_revision=1,properties={'preference':'bad'})
    assert admin.execute('select properties,nexloop_revision from ontology.objects').fetchone()==({'preference':'old'},1)


@pytest.mark.parametrize('published_action',['with-preference'],indirect=True)
def test_undeclared_property_is_rejected_before_claim(editor,admin):
    service,obj,token=editor
    count=admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]
    with pytest.raises(ValueError):
        service.edit(action_name='Consumer.edit',action_version=1,intent_id='synthetic-undeclared',type_name='Consumer',object_id=obj,expected_revision=1,properties={'unpublished':'bad'})
    assert admin.execute('select count(*) from runtime.nexloop_action_claims').fetchone()[0]==count
