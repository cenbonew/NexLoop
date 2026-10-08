from pathlib import Path
import secrets
import pytest
from nexloop_eios.model_profile import ModelConfigurationError,load_model_profile


def secret_file(tmp_path,value,name='synthetic-secret'):
    path=tmp_path/name;path.write_text(value);path.chmod(0o600);return path


def test_nonempty_secret_overrides_env_without_public_disclosure(tmp_path):
    file_key=secrets.token_urlsafe(48);env_key=secrets.token_urlsafe(48)
    path=secret_file(tmp_path,file_key+'\n')
    profile=load_model_profile({'MODEL_API_KEY':env_key,'MODEL_CREDENTIALS_FILE':str(path)})
    assert profile.credential_for_provider()==file_key
    assert profile.provider=='deepseek' and profile.model_id=='deepseek-flash'
    assert profile.credential_source=='secret_file'
    for key in (file_key,env_key):
        assert key not in repr(profile) and key not in str(profile.public_configuration())


def test_empty_secret_falls_back_locally_and_stage_rejects_env(tmp_path):
    path=secret_file(tmp_path,'\n');key=secrets.token_urlsafe(48)
    config={'MODEL_API_KEY':key,'MODEL_CREDENTIALS_FILE':str(path)}
    assert load_model_profile(config).credential_for_provider()==key
    with pytest.raises(ModelConfigurationError) as result:load_model_profile(config,stage=True)
    assert key not in str(result.value)


def test_missing_key_is_explicit_test_only_with_blocked_real_validation():
    profile=load_model_profile({'MODEL_PROVIDER':'deepseek','MODEL_API_KEY':''})
    assert profile.provider=='test' and profile.validation_mode=='test_only_real_validation_blocked'
    assert profile.credential_for_provider()=='' and profile.base_url==''


def test_local_env_load_has_no_shell_expansion_and_environment_wins(tmp_path):
    key=secrets.token_urlsafe(48);other=secrets.token_urlsafe(48)
    path=secret_file(tmp_path,'# synthetic test only\nMODEL_API_KEY='+key+'\nMODEL_ID=deepseek-flash\n','.env')
    assert load_model_profile({},env_file=path).credential_for_provider()==key
    assert load_model_profile({'MODEL_API_KEY':other},env_file=path).credential_for_provider()==other
    with pytest.raises(ModelConfigurationError):load_model_profile({},env_file=path,stage=True)


@pytest.mark.parametrize('url',['http://api.deepseek.com','https://api.deepseek.com.evil.invalid','https://api.deepseek.com:444','https://api.deepseek.com/path','https://localhost','https://user@api.deepseek.com','https://api.deepseek.com?key=hidden'])
def test_unapproved_model_egress_rejected(url):
    with pytest.raises(ModelConfigurationError):load_model_profile({'MODEL_API_KEY':secrets.token_urlsafe(48),'MODEL_BASE_URL':url})


def test_secret_symlink_permissions_and_oversize_fail_without_leak(tmp_path):
    key=secrets.token_urlsafe(48);path=secret_file(tmp_path,key)
    link=tmp_path/'link';link.symlink_to(path)
    for selected in (link,tmp_path/'missing'):
        with pytest.raises(ModelConfigurationError) as result:load_model_profile({'MODEL_CREDENTIALS_FILE':str(selected)})
        assert key not in str(result.value)
    path.chmod(0o644)
    with pytest.raises(ModelConfigurationError):load_model_profile({'MODEL_CREDENTIALS_FILE':str(path)})
    path.chmod(0o600);path.write_text('x'*16385)
    with pytest.raises(ModelConfigurationError):load_model_profile({'MODEL_CREDENTIALS_FILE':str(path)})


def test_stage_secret_and_explicit_test_ignore_env_credentials(tmp_path):
    key=secrets.token_urlsafe(48);path=secret_file(tmp_path,key)
    assert load_model_profile({'MODEL_CREDENTIALS_FILE':str(path)},stage=True).credential_for_provider()==key
    test=load_model_profile({'MODEL_PROVIDER':'test','MODEL_API_KEY':key})
    assert test.provider=='test' and test.credential_for_provider()==''
