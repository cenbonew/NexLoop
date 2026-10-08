"""Shared bounded private provider configuration; no network or automatic .env read."""
import os
import stat
import re
from pathlib import Path


class ProviderConfigurationError(ValueError):
    pass


def private_text(path, *, limit=16384):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or
                    info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_size>limit):
                raise ValueError()
            data=stream.read(limit+1)
            if len(data)>limit:raise ValueError()
            return data.decode('utf-8')
    except Exception:
        raise ProviderConfigurationError('provider configuration file rejected') from None


def configuration_values(environment=None, *, env_file=None, stage=False, prefix):
    values={}
    if env_file is not None:
        if stage:raise ProviderConfigurationError('stage must not load a development env file')
        for line in private_text(Path(env_file)).splitlines():
            if not line.strip() or line.lstrip().startswith('#'):continue
            name,separator,value=line.partition('=');name=name.strip()
            if not separator or not name.replace('_','').isalnum():
                raise ProviderConfigurationError('invalid local environment syntax')
            value=value.strip()
            if value and value[0] in "\"'":
                closing=value.find(value[0],1)
                if closing<1 or (value[closing+1:].strip() and not value[closing+1:].lstrip().startswith('#')):
                    raise ProviderConfigurationError('invalid local environment syntax')
                value=value[1:closing]
            else:
                value=re.split(r'(?:^|\s)#',value,maxsplit=1)[0].strip()
            if name.startswith(prefix+'_'):values[name]=value
    incoming=os.environ if environment is None else environment
    for name,value in incoming.items():
        if name.startswith(prefix+'_'):
            if not isinstance(value,str):raise ProviderConfigurationError('provider configuration rejected')
            values[name]=value
    return values


def provider_credential(values, *, prefix, stage=False):
    path=values.get(prefix+'_CREDENTIALS_FILE','').strip()
    key=private_text(Path(path)).strip() if path else ''
    source='secret_file' if key else 'none'
    env_key=values.get(prefix+'_API_KEY','').strip()
    if not key and env_key:
        if stage:raise ProviderConfigurationError('stage provider key requires a nonempty secret file')
        key=env_key;source='environment'
    if key and (len(key)>8192 or any(ord(char)<33 or ord(char)>126 for char in key)):
        raise ProviderConfigurationError('provider key must be bounded single-line ASCII')
    return key,source
