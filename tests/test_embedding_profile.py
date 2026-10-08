"""Synthetic credentials only; no provider/network/index operation."""
import secrets
import pytest
from nexloop_eios.embedding_profile import EmbeddingConfigurationError,load_embedding_profile
from nexloop_eios.model_profile import load_model_profile


def configured(**changes):
    return {'EMBEDDING_MODEL':'synthetic-embedding','EMBEDDING_MODE':'multimodal',
            'EMBEDDING_LOCATION':'https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal',
            'EMBEDDING_DIMENSION':'1024',**changes}


def private(tmp_path,body,name='secret'):
    path=tmp_path/name;path.write_text(body);path.chmod(0o600);return path


def test_same_rule_file_nonempty_wins_locally_and_stage_without_disclosure(tmp_path):
    file_key=secrets.token_urlsafe(48);env_key=secrets.token_urlsafe(48);path=private(tmp_path,file_key)
    for stage in (False,True):
        profile=load_embedding_profile(configured(EMBEDDING_API_KEY=env_key,EMBEDDING_CREDENTIALS_FILE=str(path)),stage=stage)
        assert profile.credential_for_provider()==file_key and profile.credential_source=='secret_file'
        assert profile.public_configuration()['dimension']==1024 and profile.validation_mode=='real_validation_pending'
        assert file_key not in repr(profile)+str(profile.public_configuration()) and env_key not in repr(profile)
    assert load_model_profile({'MODEL_API_KEY':env_key,'MODEL_CREDENTIALS_FILE':str(path)},stage=True).credential_for_provider()==file_key


def test_empty_file_local_fallback_stage_refusal(tmp_path):
    key=secrets.token_urlsafe(48);path=private(tmp_path,'\n')
    values=configured(EMBEDDING_API_KEY=key,EMBEDDING_CREDENTIALS_FILE=str(path))
    assert load_embedding_profile(values).credential_for_provider()==key
    with pytest.raises(EmbeddingConfigurationError) as result:load_embedding_profile(values,stage=True)
    assert key not in str(result.value)


def test_explicit_env_file_same_names_no_expansion_process_values_win(tmp_path):
    key=secrets.token_urlsafe(48);other=secrets.token_urlsafe(48)
    path=private(tmp_path,'\n'.join(k+'='+v for k,v in configured(EMBEDDING_API_KEY=key).items()),'local-env')
    assert load_embedding_profile({},env_file=path).credential_for_provider()==key
    assert load_embedding_profile({'EMBEDDING_API_KEY':other},env_file=path).credential_for_provider()==other
    with pytest.raises(EmbeddingConfigurationError):load_embedding_profile({},env_file=path,stage=True)
    profile=load_embedding_profile(configured(EMBEDDING_API_KEY='$(do-not-execute)'))
    assert profile.credential_for_provider()=='$(do-not-execute)'


def test_missing_values_are_not_fake_embedding_or_measured_dimensions():
    assert load_embedding_profile({}).validation_mode=='disabled'
    assert load_embedding_profile(configured()).validation_mode=='missing_credentials'
    profile=load_embedding_profile(configured(EMBEDDING_DIMENSION='',EMBEDDING_API_KEY='synthetic-key'))
    assert profile.dimension is None and profile.validation_mode=='dimension_unmeasured'
    assert load_embedding_profile({'MODEL_API_KEY':'synthetic-key'}).credential_for_provider()==''


@pytest.mark.parametrize('url',['http://ark.cn-beijing.volces.com/api/v3/embeddings','https://ark.cn-beijing.volces.com.evil.invalid/api',
 'https://user@ark.cn-beijing.volces.com/api','https://ark.cn-beijing.volces.com:444/api',
 'https://ark.cn-beijing.volces.com/api?key=synthetic','https://ark.cn-beijing.volces.com/api#secret','https://localhost/api'])
def test_location_current_maintainer_allowlist_even_without_key(url):
    with pytest.raises(EmbeddingConfigurationError):load_embedding_profile(configured(EMBEDDING_LOCATION=url))


@pytest.mark.parametrize('name,value',[('EMBEDDING_DIMENSION','0'),('EMBEDDING_DIMENSION','-1'),('EMBEDDING_DIMENSION','1024.0'),
 ('EMBEDDING_DIMENSION','01024'),('EMBEDDING_DIMENSION','65537'),('EMBEDDING_DIMENSION','١٠٢٤'),
 ('EMBEDDING_MODE','automatic'),('EMBEDDING_MODEL',''),('EMBEDDING_BASE_URL','https://other.invalid/api')])
def test_invalid_or_old_alias_configuration_rejected(name,value):
    with pytest.raises(EmbeddingConfigurationError):load_embedding_profile(configured(**{name:value}))


def test_bad_secret_never_falls_back_and_errors_hide_material(tmp_path):
    key=secrets.token_urlsafe(48);path=private(tmp_path,key);link=tmp_path/'link';link.symlink_to(path)
    for selected in (link,tmp_path/'absent'):
        with pytest.raises(EmbeddingConfigurationError) as result:
            load_embedding_profile(configured(EMBEDDING_CREDENTIALS_FILE=str(selected),EMBEDDING_API_KEY=key))
        assert key not in str(result.value) and str(selected) not in str(result.value)
    path.chmod(0o644)
    with pytest.raises(EmbeddingConfigurationError):load_embedding_profile(configured(EMBEDDING_CREDENTIALS_FILE=str(path)))
    path.chmod(0o600);path.write_text('x'*16385)
    with pytest.raises(EmbeddingConfigurationError):load_embedding_profile(configured(EMBEDDING_CREDENTIALS_FILE=str(path)))


def test_dimension_is_not_inferred_or_silently_renamed():
    profile=load_embedding_profile(configured(EMBEDDING_DIMENSION='3072',EMBEDDING_API_KEY='synthetic-key'))
    assert profile.dimension==3072 and profile.validation_mode=='real_validation_pending'


def test_blank_public_template_inline_comments_remain_blank(tmp_path):
    from pathlib import Path
    # Only the public template is read; never read the owner's .env.
    template=Path(__file__).resolve().parents[1]/'.env.example'
    path=private(tmp_path,template.read_text(),'public-template-copy')
    assert load_embedding_profile({},env_file=path).validation_mode=='disabled'


def test_hardlinked_credentials_rejected_for_both_profiles(tmp_path):
    import os
    from nexloop_eios.model_profile import ModelConfigurationError
    key=secrets.token_urlsafe(48);path=private(tmp_path,key);os.link(path,tmp_path/'second-link')
    with pytest.raises(EmbeddingConfigurationError):load_embedding_profile(configured(EMBEDDING_CREDENTIALS_FILE=str(path)))
    with pytest.raises(ModelConfigurationError):load_model_profile({'MODEL_CREDENTIALS_FILE':str(path)})


def test_real_fifo_credentials_rejected_without_blocking_process(tmp_path):
    import os,subprocess,sys
    fifo=tmp_path/'credential-fifo';os.mkfifo(fifo,0o600)
    code="""
import sys
from nexloop_eios.embedding_profile import load_embedding_profile,EmbeddingConfigurationError
from nexloop_eios.model_profile import load_model_profile,ModelConfigurationError
for load,error,prefix in [(load_embedding_profile,EmbeddingConfigurationError,'EMBEDDING'),(load_model_profile,ModelConfigurationError,'MODEL')]:
    try:load({prefix+'_CREDENTIALS_FILE':sys.argv[1]})
    except error:pass
    else:raise SystemExit(2)
print('fifo rejected')
"""
    child=subprocess.Popen([sys.executable,'-c',code,str(fifo)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        stdout,stderr=child.communicate(timeout=5)
        assert child.returncode==0 and stdout=='fifo rejected\n' and stderr==''
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=5)
