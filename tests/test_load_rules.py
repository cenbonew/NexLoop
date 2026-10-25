"""NX-031 load lane: the load-rules extraction provider (dispatcher ruling 2026-10-11).

A variant of the test model profile, admitted only in test mode, never on stage, never with a credential; the extraction
worker refuses it at startup unless it runs with --mode test, the matcher always refuses it, and the stage doctor fails it.
It only turns the synthetic promise sentence into a commitment Claim and calls no model or network."""
import json

import pytest
from nexloop_eios import background_services as B
from nexloop_eios.conversation_extraction import SYSTEM_PROMPT
from nexloop_eios.load_rules import LoadRulesExtractionProvider
from nexloop_eios.model_profile import ModelConfigurationError, load_model_profile
from test_background_services_pg import argv
from test_service_grants_pg import deployment, private  # noqa: F401
from support.service_stderr import service_stderr

VARIANT={'MODEL_PROVIDER':'test','MODEL_TEST_VARIANT':'load-rules'}


def test_profile_variant_is_test_mode_only():
    with pytest.raises(ModelConfigurationError):load_model_profile(VARIANT)  # default: refused
    profile=load_model_profile(VARIANT,allow_load_rules=True)
    assert (profile.provider,profile.model_id,profile.credential_source,profile.credential_for_provider())==('test','load-rules','none','')
    for environment,options in [(VARIANT,{'allow_load_rules':True,'stage':True}),({**VARIANT,'MODEL_TEST_VARIANT':'other'},{'allow_load_rules':True}),
                                ({**VARIANT,'MODEL_PROVIDER':'deepseek'},{'allow_load_rules':True}),({**VARIANT,'MODEL_API_KEY':'k'*40},{'allow_load_rules':True})]:
        with pytest.raises(ModelConfigurationError):load_model_profile(environment,**options)
    assert load_model_profile({'MODEL_PROVIDER':'test'}).model_id=='deterministic-test'  # the plain test profile is unchanged


def test_provider_only_reads_the_synthetic_promise_and_calls_nothing(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx,'Client',lambda *a,**k:(_ for _ in ()).throw(AssertionError('no network')))
    payload=json.dumps({'conversation_data':[{'ref':1,'speaker':'顾客','text':'我会在家等快递，请明天前回电话。'},
        {'ref':2,'speaker':'客服','text':'好的，我会在2天内给您回电话。'},{'ref':3,'speaker':'客服','text':'已为您登记。'}],'timezone':'Asia/Shanghai','output':'JSON only'})
    out=json.loads(LoadRulesExtractionProvider().complete(SYSTEM_PROMPT,payload))
    assert [c['message_ref'] for c in out['claims']]==[2] and out['claims'][0]['quote']=='我会在2天内给您回电话'
    assert out['claims'][0]['kind']=='commitment' and out['topics'][0]['message_refs']==[1,2,3]


@pytest.mark.parametrize('service,mode,started',[('claim-extraction-worker',None,False),('claim-extraction-worker','stage',False),
    ('claim-extraction-worker','test',True),('claim-matcher',None,False)])
def test_background_services_refuse_the_variant_outside_test_mode(deployment,tmp_path,capsys,service,mode,started):
    d=deployment;d['apply']()
    args=argv(d,tmp_path,service)
    model=args.index('--model-env-file')+1
    args[model]=str(private(tmp_path/service,'load-rules.env','MODEL_PROVIDER=test\nMODEL_TEST_VARIANT=load-rules\n'))
    code=B.main_for(service,args+(['--mode',mode] if mode else []))
    out,err=capsys.readouterr()
    if started:
        assert code==0 and out.startswith(B.SERVICES[service][0]+' ready') and json.loads(out.splitlines()[1])=={'status':'idle'}
    else:
        assert code==1 and out=='' and service_stderr(err)==B.SERVICES[service][0]+' unavailable\n'


def test_stage_doctor_fails_the_variant(pg,tmp_path):
    from nexloop_eios.doctor import diagnose
    (tmp_path/'artifacts').mkdir(mode=0o700)
    assert diagnose(database_url=pg,artifact_root=tmp_path/'artifacts',environment=VARIANT)['checks']['model_configuration'] is False
    assert diagnose(database_url=pg,artifact_root=tmp_path/'artifacts',environment={'MODEL_PROVIDER':'test'})['checks']['model_configuration'] is True


def test_lazy_provider_names_its_provider_and_model_for_the_input_digest(tmp_path):
    """The extraction worker's provider is resolved on first use; the extractor reads provider/model_id for the input digest
    (missing before: every non-empty window failed with an unexpected error)."""
    from nexloop_eios.conversation_extraction import ExtractionProviderUnavailable
    lazy=B.LazyModelProvider(private(tmp_path,'load.env','MODEL_PROVIDER=test\nMODEL_TEST_VARIANT=load-rules\n'),ExtractionProviderUnavailable,allow_load_rules=True)
    assert (lazy.provider,lazy.model_id)==('test','load-rules')
    refused=B.LazyModelProvider(tmp_path/'load.env',ExtractionProviderUnavailable)  # not allowed: unavailable, never a silent fallback
    with pytest.raises(ExtractionProviderUnavailable):refused.provider
    plain=B.LazyModelProvider(private(tmp_path,'test.env','MODEL_PROVIDER=test\n'),ExtractionProviderUnavailable)
    with pytest.raises(ExtractionProviderUnavailable):plain.model_id  # the plain test profile still has no extraction provider
