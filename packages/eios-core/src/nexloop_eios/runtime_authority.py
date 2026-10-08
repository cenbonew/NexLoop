"""Trusted backend guard for a durable task-bound Run.

Raw Run authority remains in backend memory. Host receives only a command and
checks through the trusted callback/transport; it never receives a database DSN.
A scheduling lease does not grant Action authority. Each guard call also freshly
authenticates the separately issued EIOS Run identity.
"""
from dataclasses import dataclass,field
from datetime import UTC,datetime
from eios.authz.errors import AuthorizationUnavailable
from nexloop_eios.postgres_artifacts import canonical_payload
from nexloop_eios.run_credentials import RunCredential

@dataclass(frozen=True)
class RuntimeAuthority:
    worker:object=field(repr=False)
    backend:object=field(repr=False)
    issued:RunCredential=field(repr=False)
    queue:str
    task_id:str
    fence:int

    def authorize(self,command,operation):
        try:
            if operation not in ('start','resume','inspect','cancel','model','tool'):raise ValueError()
            if not isinstance(command,dict) or command.get('run_id')!=self.issued.run_id:raise ValueError()
            lease=self.worker.assert_task_lease(queue=self.queue,task_id=self.task_id,fence=self.fence)
            persisted=lease['payload']['run_command']
            if canonical_payload(command)!=canonical_payload(persisted):raise ValueError()
            if command['world_id']!=lease['world'] or datetime.fromisoformat(command['not_after'].replace('Z','+00:00'))<=datetime.now(UTC):raise ValueError()
            run=self.backend.authenticate_run(self.issued.token,world=lease['world'],run_id=self.issued.run_id)
            session=run._session
            if session.authentication.tenant_id!=command['tenant_id'] or session.world!=command['world_id'] or session.run_context is None:raise ValueError()
            # The successful result is intentionally secret-free. It is a guard,
            # never a grant to emit an external effect or write ontology SQL.
            return {'authorized':True,'run_id':self.issued.run_id,'task_id':self.task_id,'fence':self.fence}
        except Exception:
            # Provider/Gateway errors must not put a token/DSN into Pi transcript.
            raise AuthorizationUnavailable('runtime authority unavailable') from None
