"""NX-021 embedding providers. The real provider is constructed only from an explicit profile; CI uses the deterministic one."""
import hashlib
import json
import math
import re
from .embedding_profile import EmbeddingConfigurationError

_CJK=re.compile('[㐀-䶿一-鿿豈-﫿]+')
_WORD=re.compile(r'[a-z0-9_]+')


class EmbeddingDimensionMismatch(RuntimeError):
    pass


class EmbeddingUnavailable(RuntimeError):
    pass


def _text(text):
    if type(text) is not str or not text.strip() or len(text)>4000:
        raise ValueError('embedding input must be bounded nonempty text')
    return text


def checked_vector(values,dimension):
    """Fail closed on any length other than the frozen profile dimension."""
    if type(values) not in (list,tuple) or len(values)!=dimension:
        raise EmbeddingDimensionMismatch('embedding dimension mismatch')
    vector=tuple(float(v) for v in values)
    if not all(math.isfinite(v) for v in vector) or not any(vector):
        raise EmbeddingUnavailable('embedding vector invalid')
    return vector


class DeterministicTestEmbeddingProvider:
    """Hashed CJK-bigram/word features, L2-normalized. Test/demo only: no model semantics claim."""

    def __init__(self,dimension=64,*,model='nexloop-test-ngram-v1'):
        if type(dimension) is not int or not 8<=dimension<=4096:raise ValueError('test dimension invalid')
        self.model,self.dimension=model,dimension
        self.calls=0;self.usage_tokens=0

    @property
    def profile_id(self):return f'{self.model}@{self.dimension}'

    def embed(self,text):
        text=_text(text).lower();self.calls+=1;self.usage_tokens+=len(text)
        features=[w for w in _WORD.findall(_CJK.sub(' ',text))]
        for run in _CJK.findall(text):
            features+=[run] if len(run)==1 else [run[i:i+2] for i in range(len(run)-1)]
        vector=[0.0]*self.dimension
        for feature in features or [text]:
            digest=hashlib.sha256(feature.encode()).digest()
            vector[int.from_bytes(digest[:4],'big')%self.dimension]+=1.0 if digest[4]&1 else -1.0
        norm=math.sqrt(sum(v*v for v in vector)) or 1.0
        if not any(vector):vector[0]=1.0;norm=1.0
        return checked_vector([v/norm for v in vector],self.dimension)


def _deadline_transport(url,headers,body,timeout):
    """D2 (eios-capability-gap): `timeout` is the whole request's hard deadline (connect, TLS, write and read together) in a
    killable child process; a timeout is DeadlineExceeded (an OSError, retried within `attempts`: an embedding has no effect)."""
    from nexloop_eios.deadline_http import post
    return post(url,headers,body,deadline_seconds=timeout)


class ArkMultimodalEmbeddingProvider:
    """Volcengine Ark multimodal embeddings, text input only (NX-021 probe).

    One text per request: several inputs are fused into a single vector by the
    endpoint. Only transport failures are retried (bounded); HTTP errors are not.
    The key is never placed in messages or exceptions.
    """

    def __init__(self,profile,*,transport=None,timeout=10.0,attempts=3):
        if profile.validation_mode in ('disabled','missing_credentials'):
            raise EmbeddingConfigurationError('embedding provider not configured')
        if profile.dimension is None:
            raise EmbeddingConfigurationError('EMBEDDING_DIMENSION must be measured and set before use')
        if profile.mode!='multimodal' or not profile.location.endswith('/embeddings/multimodal'):
            raise EmbeddingConfigurationError('only the verified multimodal text path is supported')
        if not 1<=attempts<=5 or not 0<timeout<=60:raise ValueError('bounded retry/timeout required')
        self._profile=profile;self._transport=transport or _deadline_transport
        self.model,self.dimension=profile.model,profile.dimension
        self._timeout,self._attempts=timeout,attempts
        self.transport_failures=0
        # 0219 budget: provider-reported input tokens and calls of this process (read and reset by EmbeddingBudget).
        self.usage_tokens=0;self.calls=0

    @property
    def profile_id(self):return f'{self.model}@{self.dimension}'

    def embed(self,text):
        body=json.dumps({'model':self.model,'input':[{'type':'text','text':_text(text)}],
            'dimensions':self.dimension,'encoding_format':'float'},ensure_ascii=False).encode()
        headers={'Content-Type':'application/json','Authorization':'Bearer '+self._profile.credential_for_provider()}
        for attempt in range(self._attempts):
            try:
                status,raw=self._transport(self._profile.location,headers,body,self._timeout)
                break
            except OSError:
                self.transport_failures+=1
                if attempt+1==self._attempts:raise EmbeddingUnavailable('embedding transport unavailable') from None
        try:payload=json.loads(raw)
        except Exception:raise EmbeddingUnavailable(f'embedding provider HTTP {status} non-JSON') from None
        if status!=200:
            code=payload.get('error',{}).get('code') if isinstance(payload,dict) and isinstance(payload.get('error'),dict) else None
            code=code if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.]{1,80}',code) else 'unknown'
            raise EmbeddingUnavailable(f'embedding provider HTTP {status} {code}')
        self.calls+=1
        usage=payload.get('usage') if isinstance(payload,dict) else None
        details=usage.get('prompt_tokens_details') if isinstance(usage,dict) else None
        # Ark prices text and image input tokens separately (owner 2026-10-18: text 0.70 CNY, image 1.80 CNY per million). Only
        # text is sent, so the text tokens are counted; a response without usage is counted at one token per character (an upper
        # bound for the budget, never zero).
        tokens=details.get('text_tokens') if isinstance(details,dict) and 'text_tokens' in details else (
            usage.get('prompt_tokens',usage.get('total_tokens')) if isinstance(usage,dict) else None)
        self.usage_tokens+=tokens if type(tokens) is int and 0<=tokens<=1_000_000 else len(text)
        image=details.get('image_tokens',0) if isinstance(details,dict) else 0
        if image not in (0,None):
            # A text-only request billed as image input: counted (at least) and refused, never used silently.
            self.usage_tokens+=image if type(image) is int and 0<image<=1_000_000 else len(text)
            raise EmbeddingUnavailable('embedding provider reported image tokens for a text input')
        data=payload.get('data') if isinstance(payload,dict) else None
        if not isinstance(data,dict) or 'embedding' not in data:
            raise EmbeddingUnavailable('embedding response shape unexpected')
        return checked_vector(data['embedding'],self.dimension)
