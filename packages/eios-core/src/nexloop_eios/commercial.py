"""NX-027 commercial events (M08): docs/implementation/NX-027-design.md, migration 0130.

A configured connector (deployment configuration through nexloop_configurator; the HMAC key comes from a private file
and is held by nexloop_owner) is bound to one tenant, world and data mode: a test connector only writes a non-real world,
a real one needs the owner's confirmation (D1, D2). ``POST /api/v1/webhooks/commercial/{connector}`` (commercial_http)
passes the raw body, timestamp and signature to ``authz.nexloop_commercial_ingest``; only verified first arrivals are
stored and mark the record in the 'commercial-record' work feed in the same transaction.

``CommercialRecorder`` (service principal commercial_recorder, nexloop_domain_worker) drains that feed: SQL prepares the
one CommercialRecord state all verified events of the record justify (order independent: late older events never move
it back, provider corrections and refunds included), the state is applied through the governed CommercialRecord.create
/ CommercialRecord.edit Actions and the SQL object guard accepts exactly that state; ``recorded`` dispositions the
events (applied / late) and runs the downstream hook (commitments, plans). A customer's own words never reach this
chain (AT-011). ``CommercialReadPort`` is the query port before NX-028.
"""
import hashlib
import hmac
import json
from pathlib import Path

import psycopg
from eios.authz import facts as F
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.assembly import verify_application_role
from nexloop_eios.object_actions import GovernedObjectCreator
from nexloop_eios.object_edits import GovernedObjectEditor
from nexloop_eios.plan_reevaluation import PlanUnavailable,_SignedPort
from nexloop_eios.private_configuration import read_private_text
from nexloop_eios.work_feed import WorkFeed,WorkFeedDenied,retry_delay

RECORD_ACTION='eios:action:nexloop.commercial.record:1'
READ_ACTION='eios:action:nexloop.commercial.read:1'
RECORD_FEED='commercial-record'
TYPE='CommercialRecord'
CREATE=('CommercialRecord.create',1)
EDIT=('CommercialRecord.edit',1)
SETTINGS_KEYS={'schema','version','decision','event_types','currency_exponents','replay_window_seconds','max_payload_bytes',
    'max_future_skew_seconds','financial_retention_days','channel_unit_rates','worker'}
_WORKER={'batch':(1,50),'lease_seconds':(10,600),'max_attempts':(1,20),'retry_base_seconds':(1,3600)}
_KINDS={'order':{'pending','paid','cancelled'},'payment':{'pending','succeeded','failed'},'renewal':{'succeeded'},'refund':{'succeeded'}}


def load_settings(path):
    """Deployment configuration (deploy/configuration/commercial.v1.json); every value is bounded."""
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if type(value) is not dict or set(value)!=SETTINGS_KEYS:raise ValueError('commercial settings shape')
    if value['schema']!='nexloop-commercial/1' or type(value['version']) is not int or value['version']<1:raise ValueError('commercial settings version')
    if type(value['decision']) is not str or not value['decision'].strip():raise ValueError('commercial settings decision')
    types=value['event_types']
    if type(types) is not dict or not types:raise ValueError('event_types')
    for name,entry in types.items():
        if (type(entry) is not dict or set(entry)!={'kind','status'} or entry['kind'] not in _KINDS or entry['status'] not in _KINDS[entry['kind']]
            or not name.startswith(entry['kind'] if entry['kind']!='renewal' else 'subscription')):raise ValueError('event type '+name)
    exponents=value['currency_exponents']
    if type(exponents) is not dict or not exponents or any(len(k)!=3 or not k.isupper() or type(v) is not int or not 0<=v<=4 for k,v in exponents.items()):
        raise ValueError('currency_exponents')
    bounds={'replay_window_seconds':(30,3600),'max_payload_bytes':(1024,262144),'max_future_skew_seconds':(0,3600),'financial_retention_days':(1,36500)}
    for key,(low,high) in bounds.items():
        # D6: financial_retention_days must be a positive integer (default 365, enterprise-configurable).
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError(key)
    rates=value['channel_unit_rates']
    if type(rates) is not list:raise ValueError('channel_unit_rates')
    for rate in rates:
        if (type(rate) is not dict or set(rate)!={'action','version','currency','amount_per_unit'} or type(rate['version']) is not int
            or rate['currency'] not in exponents or type(rate['amount_per_unit']) is not str):raise ValueError('channel_unit_rates entry')
    worker=value['worker']
    if type(worker) is not dict or set(worker)!=set(_WORKER):raise ValueError('commercial worker settings')
    for key,(low,high) in _WORKER.items():
        if type(worker[key]) is not int or not low<=worker[key]<=high:raise ValueError('commercial worker '+key)
    return value


def canonical_settings(path):
    """Byte-equal canonical JSON of the settings file (the migration seeds the same text and digest)."""
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    text=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return text,hashlib.sha256(text.encode()).hexdigest()


def sign(key,timestamp,body):
    """Connector signature header value: HMAC-SHA256 over '<timestamp>.<raw body>' (v1)."""
    if type(body) is str:body=body.encode()
    return 'v1='+hmac.new(key,str(timestamp).encode()+b'.'+body,'sha256').hexdigest()


class CommercialPort(_SignedPort):
    """Record keeper port (eios:action:nexloop.commercial.record:1)."""
    PROTOCOL='nexloop-commercial-record-v1';ACTION=RECORD_ACTION;FUNCTION='nexloop_commercial_command'

    def prepare(self,record_key):return self._signed({'verb':'prepare','record_key':str(record_key)})
    def recorded(self,record_key):return self._signed({'verb':'recorded','record_key':str(record_key)})


class CommercialReadPort(_SignedPort):
    """Records with their events and exceptions, receipt counts (eios:action:nexloop.commercial.read:1)."""
    PROTOCOL='nexloop-commercial-read-v1';ACTION=READ_ACTION;FUNCTION='nexloop_commercial_read'

    def records(self,consumer_id=None):
        payload={'verb':'records'}
        if consumer_id is not None:payload['consumer_id']=str(consumer_id)
        return self._signed(payload)['records']

    def record(self,record_id):return self._signed({'verb':'record','record_id':str(record_id)})
    def exceptions(self):return self._signed({'verb':'exceptions'})['exceptions']
    def receipts(self):return self._signed({'verb':'receipts'})['receipts']


def _code(error):
    if isinstance(error,PlanUnavailable):return 'port_unavailable'
    if isinstance(error,(PermissionError,F.AuthorizationFactDenied,AuthorizationUnavailable)):return 'denied'
    if isinstance(error,psycopg.errors.SerializationFailure):return 'changed'
    return 'commercial_failed'


class CommercialRecorder:
    """One tick over the 'commercial-record' feed: derive, apply through the governed Actions, record."""

    def __init__(self,pool,session,signer,*,settings):
        self.pool,self.session,self.signer=pool,session,signer
        self.worker=settings['worker']
        self.port=CommercialPort(pool,session,signer)
        self.feed=WorkFeed(pool,session,signer,feed=RECORD_FEED)

    def apply(self,record_key):
        prepared=self.port.prepare(record_key)
        if prepared['action']=='create':
            GovernedObjectCreator(self.pool,self.session,self.signer).create(action_name=CREATE[0],action_version=CREATE[1],
                intent_id=prepared['intent_id'],type_name=TYPE,properties=prepared['properties'])
        elif prepared['action']=='edit':
            GovernedObjectEditor(self.pool,self.session,self.signer).edit(action_name=EDIT[0],action_version=EDIT[1],
                intent_id=prepared['intent_id'],type_name=TYPE,object_id=prepared['object_id'],expected_revision=prepared['revision'],
                properties=prepared['patch'])
        recorded=self.port.recorded(record_key)
        return prepared['action'],recorded

    def run_once(self):
        summary={'created':0,'edited':0,'unchanged':0,'late':0,'retry':0,'dead_lettered':0,'changed':0,'lease_lost':0}
        for item in self.feed.claim(limit=self.worker['batch'],lease_seconds=self.worker['lease_seconds']):
            try:action,recorded=self.apply(item['payload']['record_key'])
            except WorkFeedDenied:raise
            except Exception as error:
                status=self.feed.retry(item_key=item['item_key'],fence=item['fence'],code=_code(error),
                    delay_seconds=retry_delay(item['attempts'],self.worker['retry_base_seconds']),max_attempts=self.worker['max_attempts'])
                summary['retry' if status=='pending' else status]+=1
                continue
            summary[{'create':'created','edit':'edited'}.get(action,'unchanged')]+=1
            summary['late']+=recorded.get('late',0)
            status=self.feed.complete(item_key=item['item_key'],fence=item['fence'])
            if status!='completed':summary[status]+=1
        return summary


# -- Deployment configuration (nexloop_configurator; trusted, private files) -----------------------------------------
def _configurator(database_url_file):
    dsn=read_private_text(database_url_file,maximum=16384)
    connection=psycopg.connect(dsn,autocommit=True)
    try:
        if connection.execute('select session_user').fetchone()[0]!='nexloop_configurator':raise PermissionError('configurator role required')
    except Exception:
        connection.close();raise
    return connection


def configure_connector(database_url_file,*,tenant,world,connector_id,data_mode,event_types,currencies,key_id,key_file,
                        owner_confirmation=None,rotation_seconds=86400):
    """Create or rotate a connector; the key is read from a private file and never returned or logged."""
    key=bytes.fromhex(read_private_text(key_file,maximum=256))
    with _configurator(database_url_file) as c:
        return c.execute('select control.nexloop_commercial_configure_connector(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (tenant,world,connector_id,data_mode,list(event_types),list(currencies),key_id,key,owner_confirmation,rotation_seconds)).fetchone()[0]


def disable_connector(database_url_file,connector_id):
    with _configurator(database_url_file) as c:
        return c.execute('select control.nexloop_commercial_disable_connector(%s)',(connector_id,)).fetchone()[0]


def link_customer(database_url_file,connector_id,customer_ref,consumer_id):
    with _configurator(database_url_file) as c:
        return c.execute('select control.nexloop_commercial_link_customer(%s,%s,%s)',(connector_id,customer_ref,consumer_id)).fetchone()[0]


def unlink_customer(database_url_file,connector_id,customer_ref):
    with _configurator(database_url_file) as c:
        return c.execute('select control.nexloop_commercial_unlink_customer(%s,%s)',(connector_id,customer_ref)).fetchone()[0]


def ingest(pool,connector_id,timestamp,signature,body):
    """The API's call of the signed intake (nexloop_api); returns {'status', 'outcome', ...}."""
    with pool.connection() as db,db.transaction():
        verify_application_role(db)
        return db.execute('select authz.nexloop_commercial_ingest(%s,%s,%s,%s)',(connector_id,timestamp,signature,body)).fetchone()[0]
