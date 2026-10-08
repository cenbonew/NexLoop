"""Secret-safe model profile loading for Host startup; no provider/network call."""
from dataclasses import dataclass,field
from pathlib import Path
import os
import stat


class ModelConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    model_id: str
    base_url: str
    validation_mode: str
    credential_source: str
    _api_key: str=field(repr=False,compare=False)

    def public_configuration(self):
        return {'provider':self.provider,'model_id':self.model_id,'base_url':self.base_url,
                'validation_mode':self.validation_mode,'credential_source':self.credential_source}

    def credential_for_provider(self):
        """Trusted Host adapter only; never include this value in a Run/prompt."""
        return self._api_key


def _private_text(path,*,limit=16384):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode&0o077
                    or info.st_uid!=os.geteuid() or info.st_size>limit):
                raise ValueError()
            data=stream.read(limit+1)
            if len(data)>limit:raise ValueError()
            return data.decode('utf-8')
    except Exception:
        # Never echo the path, source bytes, native exception or candidate key.
        raise ModelConfigurationError('model configuration file must be private, owned and bounded') from None


def _env_file(path):
    result={}
    for line in _private_text(path).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        name,separator,value=line.partition('=')
        name=name.strip()
        if not separator or not name.replace('_','').isalnum():
            raise ModelConfigurationError('invalid local environment syntax')
        value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in "\"'":value=value[1:-1]
        if name.startswith('MODEL_'):result[name]=value
    return result


def load_model_profile(environment=None,*,env_file=None,stage=False):
    """Explicit file selection; process environment overrides local .env values.

    Local empty mounted secret falls back to env. Stage never takes a key from
    environment/.env, and an absent key gives an explicitly labelled test mode.
    No shell expansion or URL supplied by a model is evaluated.
    """
    values={}
    if env_file is not None:
        if stage:raise ModelConfigurationError('stage must not load a development env file')
        values.update(_env_file(Path(env_file)))
    values.update(os.environ if environment is None else environment)
    path=values.get('MODEL_CREDENTIALS_FILE','').strip()
    key=_private_text(Path(path)).strip() if path else ''
    source='secret_file' if key else 'none'
    env_key=values.get('MODEL_API_KEY','').strip()
    if not key and env_key:
        if stage:raise ModelConfigurationError('stage model key requires a nonempty secret file')
        key=env_key;source='environment'
    if key and (len(key)>8192 or any(ord(char)<33 or ord(char)>126 for char in key)):
        raise ModelConfigurationError('model key must be bounded single-line ASCII')
    provider=values.get('MODEL_PROVIDER','').strip() or ('deepseek' if key else 'test')
    if provider not in ('deepseek','test'):
        raise ModelConfigurationError('unsupported model provider')
    if provider=='test' or not key:
        return ModelProfile('test','deterministic-test','', 'test_only_real_validation_blocked','none','')
    model=values.get('MODEL_ID','').strip() or 'deepseek-flash'
    base=values.get('MODEL_BASE_URL','').strip() or 'https://api.deepseek.com'
    if model!='deepseek-flash' or base.rstrip('/')!='https://api.deepseek.com':
        raise ModelConfigurationError('model profile is outside the configured maintainer allowlist')
    return ModelProfile('deepseek',model,'https://api.deepseek.com','real_validation_pending',source,key)
