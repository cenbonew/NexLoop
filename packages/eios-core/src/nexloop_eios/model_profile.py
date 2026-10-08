"""Secret-safe model profile loading for Host startup; no provider/network call."""
from dataclasses import dataclass,field
from .provider_credentials import ProviderConfigurationError,configuration_values,provider_credential


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


def load_model_profile(environment=None,*,env_file=None,stage=False):
    """Explicit file selection; process environment overrides local .env values.

    Local empty mounted secret falls back to env. Stage never takes a key from
    environment/.env, and an absent key gives an explicitly labelled test mode.
    No shell expansion or URL supplied by a model is evaluated.
    """
    try:
        values=configuration_values(environment,env_file=env_file,stage=stage,prefix='MODEL')
        key,source=provider_credential(values,prefix='MODEL',stage=stage)
    except ProviderConfigurationError:
        raise ModelConfigurationError('model configuration rejected') from None
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
