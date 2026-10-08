"""Actual authenticated Backend composition; no success callbacks or business SQL writes."""
import pytest
from psycopg.conninfo import make_conninfo
from test_action_definitions import published_action
from test_assessment_candidate import assessment
from nexloop_eios.backend import open_backend,BackendClosed
from nexloop_eios.authorization import authenticate_service
from nexloop_eios.postgres_action_claims import ActionAuthorizationDenied
from eios.authz import facts as F
from authority_fixture import replace_fact


def backend_options(port,pg,tmp_path):
    key=tmp_path/'synthetic-backend-authority';key.write_bytes(port.signer.material);key.chmod(0o600)
    return dict(database_url=make_conninfo(pg,user='nexloop_api'),artifact_root=tmp_path/'owned-artifacts',
      signing_key_file=key,signing_key_id=port.signer.key_id)


def test_authenticated_backend_actual_create_correct_projection_replay_and_close(assessment,pg,admin,tmp_path):
    port,creator,p,obj,token=assessment
    with open_backend(**backend_options(port,pg,tmp_path)) as backend:
        services=backend.authenticate(token,world='real')
        prior=services.read_relationship_assessment(object_id=obj)
        assert prior['formal']==[] and prior['evidence'][0]['revision']==1
        args=dict(action_name='RelationshipAssessment.create',action_version=1,intent_id='backend-create',properties=p)
        receipt=services.create_relationship_assessment(**args)
        assert services.create_relationship_assessment(**args)==receipt
        correction=dict(action_name='RelationshipAssessment.correct',action_version=1,intent_id='backend-correct',
          object_id=obj,expected_revision=1,properties=p|{'corrects_revision':1,'conclusion':'still an unverified revised hypothesis'})
        assert services.correct_relationship_assessment(**correction)['revision']==2
        assert services.correct_relationship_assessment(**correction)['revision']==2
        current=services.read_relationship_assessment(object_id=obj)
        assert current['formal']==[] and current['evidence'][0]['revision']==2
        assert current['evidence'][0]['properties']['conclusion']==correction['properties']['conclusion']
        first,second=[row[0] for row in admin.execute('select recorded_at from ontology.nexloop_assessment_revisions where assessment_id=%s order by revision',(obj,)).fetchall()]
        past=services.read_relationship_assessment_history(object_id=obj,known_at=first,valid_at=second)
        present=services.read_relationship_assessment_history(object_id=obj,known_at=second,valid_at=second)
        assert past['formal']==[] and past['evidence'][0]['revision']==1
        assert present['formal']==[] and present['evidence'][0]['revision']==2
        assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions where assessment_id=%s',(obj,)).fetchone()[0]==2
    for call in (lambda:services.read_relationship_assessment(object_id=obj),lambda:services.create_relationship_assessment(**args),lambda:services.correct_relationship_assessment(**correction),lambda:services.read_relationship_assessment_history(object_id=obj,known_at=first,valid_at=second)):
        with pytest.raises(BackendClosed):call()


def test_backend_current_property_revocation_denies_fresh_projection(assessment,pg,admin,tmp_path):
    port,creator,p,obj,token=assessment
    principal=port.session.authentication.subject_principal_id
    replace_fact(admin,'synthetic-a','grants',[principal,'eios:property:RelationshipAssessment/'+obj+'/conclusion'],F.GrantFacts,grants=[])
    with open_backend(**backend_options(port,pg,tmp_path)) as backend:
        services=backend.authenticate(token,world='real')
        with pytest.raises(ActionAuthorizationDenied,match='^object/property read denied$'):
            services.read_relationship_assessment(object_id=obj)
        from datetime import datetime,UTC
        with pytest.raises(ActionAuthorizationDenied,match='^object/property read denied$'):
            services.read_relationship_assessment_history(object_id=obj,known_at=datetime.now(UTC),valid_at=datetime.now(UTC))

@pytest.mark.parametrize('invalid',[0,False,'','2026-10-09T00:00:00Z'])
def test_backend_invalid_valid_time_cannot_silently_select_current(assessment,pg,admin,tmp_path,invalid):
    from datetime import datetime,UTC
    port,creator,p,obj,token=assessment
    with open_backend(**backend_options(port,pg,tmp_path)) as backend:
        services=backend.authenticate(token,world='real')
        with pytest.raises(ValueError,match='^timezone required$'):
            services.read_relationship_assessment(object_id=obj,valid_at=invalid)
        with pytest.raises(ValueError,match='^timezone required$'):
            services.read_relationship_assessment_history(object_id=obj,known_at=datetime.now(UTC),valid_at=invalid)
        assert admin.execute('select count(*) from ontology.nexloop_assessment_revisions where assessment_id=%s',(obj,)).fetchone()[0]==1
