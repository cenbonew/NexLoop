"""ADR-019 embedding configuration only. Does not invoke or validate a provider."""
from dataclasses import dataclass,field
from urllib.parse import urlsplit
from .provider_credentials import ProviderConfigurationError,configuration_values,provider_credential


class EmbeddingConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class EmbeddingProfile:
    model: str
    mode: str
    location: str
    dimension: int|None
    validation_mode: str
    credential_source: str
    _api_key: str=field(repr=False,compare=False)

    def public_configuration(self):
        return {key:getattr(self,key) for key in ('model','mode','location','dimension','validation_mode','credential_source')}

    def credential_for_provider(self):
        """Future trusted embedding adapter only; never expose to Runtime/browser."""
        return self._api_key


def load_embedding_profile(environment=None, *, env_file=None, stage=False,
                           allowed_hosts=frozenset({'ark.cn-beijing.volces.com'})):
    """Only the six documented EMBEDDING_* names; no MODEL_* substitution.

    Nonempty owned secret overrides environment. An empty local file may fall
    back to environment; stage rejects that fallback. Dimension is explicitly
    unmeasured when blank and must not be used to create an index.
    """
    try:
        values=configuration_values(environment,env_file=env_file,stage=stage,prefix='EMBEDDING')
        if set(values)-{'EMBEDDING_'+name for name in ('API_KEY','MODEL','MODE','LOCATION','DIMENSION','CREDENTIALS_FILE')}:
            raise ValueError()
        key,source=provider_credential(values,prefix='EMBEDDING',stage=stage)
        model=values.get('EMBEDDING_MODEL','').strip();mode=values.get('EMBEDDING_MODE','').strip()
        location=values.get('EMBEDDING_LOCATION','').strip();raw=values.get('EMBEDDING_DIMENSION','').strip()
        if not any((model,mode,location,raw,key)):
            return EmbeddingProfile('','','',None,'disabled','none','')
        if not model or len(model)>160 or any(ord(c)<33 or ord(c)>126 for c in model) or mode not in ('text','multimodal'):
            raise ValueError()
        url=urlsplit(location)
        if (url.scheme!='https' or not url.hostname or url.hostname not in allowed_hosts or
                url.username is not None or url.password is not None or url.port is not None or
                url.query or url.fragment or not url.path.startswith('/') or url.path=='/' or
                any(ord(c)<33 or ord(c)>126 for c in location) or len(location)>2048):raise ValueError()
        if raw and (not raw.isascii() or not raw.isdecimal() or str(int(raw))!=raw or not 1<=int(raw)<=65536):raise ValueError()
        dimension=int(raw) if raw else None
        status='real_validation_pending' if key and dimension else ('dimension_unmeasured' if key else 'missing_credentials')
        return EmbeddingProfile(model,mode,location,dimension,status,source,key)
    except (ProviderConfigurationError,ValueError,TypeError,AttributeError):
        raise EmbeddingConfigurationError('embedding configuration rejected') from None
