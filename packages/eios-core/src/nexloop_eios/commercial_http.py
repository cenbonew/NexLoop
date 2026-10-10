"""NX-027 signed commercial webhook (D09: POST /webhooks/{connector_id}); no session, the signature authenticates.

The body is passed to SQL as received (UTF-8 text; the HMAC covers '<timestamp>.<raw body>'). Size, replay window,
signature, schema and event deduplication are decided in ``authz.nexloop_commercial_ingest``; here only an early size
bound and a per-connector rate bound. Responses carry no payload, key, tenant or reason beyond the public outcome.
"""
import threading
import time
import uuid

import psycopg
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse

from eios.adapters.postgres.database import StorageUnavailable
from nexloop_eios.backend import BackendClosed
from nexloop_eios.commercial import ingest

MAX_BODY=262144
_STATUS={202:'accepted',200:'duplicate',400:'invalid_event',401:'unauthenticated',403:'connector_disabled',404:'not_found',409:'event_conflict',413:'too_large'}


class _RateBound:
    """Token bucket per connector id (in-process; a bound against floods, not an authority)."""

    def __init__(self,rate=20.0,burst=100):
        self.rate,self.burst=rate,burst;self._buckets={};self._lock=threading.Lock()

    def allow(self,key):
        now=time.monotonic()
        with self._lock:
            tokens,last=self._buckets.get(key,(self.burst,now))
            tokens=min(self.burst,tokens+(now-last)*self.rate)
            if tokens<1:self._buckets[key]=(tokens,now);return False
            self._buckets[key]=(tokens-1,now);return True


def router(*,rate=20.0,burst=100):
    api=APIRouter();bound=_RateBound(rate,burst)

    def reply(status,code):
        return JSONResponse(status_code=status,content={'code':code,'message':'Resource unavailable' if status>=400 else 'Accepted',
            'trace_id':str(uuid.uuid4()),'retryable':status in (429,503),'details':{}},headers={'Cache-Control':'no-store'})

    @api.post('/api/v1/webhooks/commercial/{connector_id}')
    async def commercial_event(connector_id:str,request:Request):
        if len(connector_id)>63:return reply(404,'not_found')
        length=request.headers.get('content-length')
        if length is None or not length.isdigit() or int(length)>MAX_BODY:return reply(413,'too_large')
        if request.headers.get('content-type','').split(';')[0].strip()!='application/json':return reply(400,'invalid_event')
        if not bound.allow(connector_id):return reply(429,'rate_limited')
        raw=await request.body()
        if len(raw)>MAX_BODY:return reply(413,'too_large')
        try:body=raw.decode('utf-8')
        except UnicodeDecodeError:return reply(400,'invalid_event')
        backend=getattr(request.app.state,'backend',None)
        if backend is None:return reply(503,'dependency_unavailable')
        try:
            result=ingest(backend._pool,connector_id,request.headers.get('x-nexloop-timestamp'),request.headers.get('x-nexloop-signature'),body)
        except (StorageUnavailable,BackendClosed,psycopg.OperationalError,psycopg.InterfaceError):return reply(503,'dependency_unavailable')
        except Exception:return reply(503,'dependency_unavailable')
        status=int(result.get('status',503))
        return reply(status,_STATUS.get(status,'dependency_unavailable'))

    return api
