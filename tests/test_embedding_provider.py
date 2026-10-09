"""NX-021 embedding providers without network: deterministic test provider and the Ark adapter over a fake transport."""
import json
import math
import secrets
import pytest
from nexloop_eios.embedding_profile import EmbeddingConfigurationError,load_embedding_profile
from nexloop_eios.embedding_provider import (ArkMultimodalEmbeddingProvider,DeterministicTestEmbeddingProvider,
    EmbeddingDimensionMismatch,EmbeddingUnavailable)

LOCATION='https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal'


def profile(dimension='1024',mode='multimodal',location=LOCATION):
    key='synthetic-'+secrets.token_hex(16)
    return load_embedding_profile({'EMBEDDING_API_KEY':key,'EMBEDDING_MODEL':'doubao-embedding-vision-251215','EMBEDDING_MODE':mode,
        'EMBEDDING_LOCATION':location,'EMBEDDING_DIMENSION':dimension}),key


class Transport:
    def __init__(self,*responses):self.responses=list(responses);self.requests=[]

    def __call__(self,url,headers,body,timeout):
        self.requests.append((url,headers,json.loads(body),timeout))
        response=self.responses.pop(0)
        if isinstance(response,Exception):raise response
        return response


def ok(dimension):return 200,json.dumps({'data':{'embedding':[0.5]*dimension,'object':'embedding'},'object':'list'}).encode()


def test_deterministic_provider_is_stable_normalized_and_lexically_meaningful():
    provider=DeterministicTestEmbeddingProvider(64)
    a=provider.embed('关注防水功能');assert a==provider.embed('关注防水功能') and len(a)==64
    assert math.isclose(sum(v*v for v in a),1.0,rel_tol=1e-9)
    cos=lambda x,y:sum(p*q for p,q in zip(x,y))
    assert cos(a,provider.embed('在意是否防水'))>cos(a,provider.embed('预算两千元以内'))
    assert provider.profile_id=='nexloop-test-ngram-v1@64'
    for bad in ('',' ','x'*4001,None):
        with pytest.raises(ValueError):provider.embed(bad)


def test_ark_request_shape_is_one_text_with_fixed_dimension_and_bearer_key():
    config,key=profile();transport=Transport(ok(1024))
    provider=ArkMultimodalEmbeddingProvider(config,transport=transport)
    vector=provider.embed('预算两千元以内')
    assert len(vector)==1024 and provider.profile_id=='doubao-embedding-vision-251215@1024'
    url,headers,body,timeout=transport.requests[0]
    assert url==LOCATION and headers['Authorization']=='Bearer '+key and 0<timeout<=60
    assert body=={'model':'doubao-embedding-vision-251215','input':[{'type':'text','text':'预算两千元以内'}],'dimensions':1024,'encoding_format':'float'}
    assert key not in repr(provider) and key not in repr(config)


def test_ark_dimension_mismatch_and_unmeasured_dimension_fail_closed():
    config,_=profile()
    with pytest.raises(EmbeddingDimensionMismatch):ArkMultimodalEmbeddingProvider(config,transport=Transport(ok(2048))).embed('关注防水功能')
    unmeasured,_=profile(dimension='')
    with pytest.raises(EmbeddingConfigurationError,match='measured'):ArkMultimodalEmbeddingProvider(unmeasured)
    text_mode,_=profile(mode='text',location='https://ark.cn-beijing.volces.com/api/v3/embeddings')
    with pytest.raises(EmbeddingConfigurationError):ArkMultimodalEmbeddingProvider(text_mode)
    with pytest.raises(EmbeddingConfigurationError):ArkMultimodalEmbeddingProvider(load_embedding_profile({}))


def test_ark_http_errors_are_not_retried_and_never_echo_the_key():
    config,key=profile()
    error=json.dumps({'error':{'code':'InvalidParameter','message':'request id and '+key,'param':'dimensions','type':'BadRequest'}}).encode()
    transport=Transport((400,error),ok(1024))
    with pytest.raises(EmbeddingUnavailable) as raised:ArkMultimodalEmbeddingProvider(config,transport=transport).embed('关注防水功能')
    assert str(raised.value)=='embedding provider HTTP 400 InvalidParameter' and key not in str(raised.value)
    assert len(transport.requests)==1
    with pytest.raises(EmbeddingUnavailable,match='HTTP 401 unknown'):
        ArkMultimodalEmbeddingProvider(config,transport=Transport((401,b'{"error":{"code":"Bad code with spaces"}}'))).embed('x')
    with pytest.raises(EmbeddingUnavailable,match='shape'):ArkMultimodalEmbeddingProvider(config,transport=Transport((200,b'{"data":[]}'))).embed('x')
    with pytest.raises(EmbeddingUnavailable,match='invalid'):
        ArkMultimodalEmbeddingProvider(config,transport=Transport((200,json.dumps({'data':{'embedding':[0.0]*1024}}).encode()))).embed('x')


def test_ark_transport_failures_bounded_retry():
    config,_=profile()
    flaky=Transport(OSError('tls eof'),ok(1024))
    provider=ArkMultimodalEmbeddingProvider(config,transport=flaky)
    assert len(provider.embed('关注防水功能'))==1024 and provider.transport_failures==1 and len(flaky.requests)==2
    down=Transport(OSError('a'),OSError('b'),OSError('c'),ok(1024))
    with pytest.raises(EmbeddingUnavailable,match='transport') as raised:ArkMultimodalEmbeddingProvider(config,transport=down,attempts=3).embed('x')
    assert len(down.requests)==3 and raised.value.__cause__ is None
    with pytest.raises(ValueError):ArkMultimodalEmbeddingProvider(config,attempts=9)
