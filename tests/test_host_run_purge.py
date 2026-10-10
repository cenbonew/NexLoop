"""NX-029 D8 on the actual Agent Host: the retention keeper's HostPurger removes one Run directory over the loopback
internal API (internal key, TLS to 127.0.0.1). A missing directory is absent, a wrong key or a malformed id is a
failure (the queue keeps the item); the purge needs no guard call and never touches another Run's directory."""
import uuid

from nexloop_eios.retention import HostPurger
from test_agent_host import files
from test_runtime_host_admission import configuration,guard_server,host


class DeniedWorker:
    # The purge makes no guard call; any guard request would be denied.
    def authorize_runtime_activation(self,**kwargs):raise RuntimeError('synthetic denial')


def test_host_purges_one_run_directory_over_loopback(tmp_path):
    runtime,key=files(tmp_path);guard_key=tmp_path/'guard-key';guard_key.write_text('b'*64);guard_key.chmod(0o600)
    run,other=str(uuid.uuid4()),str(uuid.uuid4())
    for r in (run,other):
        (runtime/r).mkdir(mode=0o700);(runtime/r/'runtime.sqlite').write_bytes(b'synthetic')
    with guard_server(DeniedWorker(),tmp_path,guard_key) as port:
        with host(runtime,key,configuration(tmp_path,port,guard_key)) as (_,client,headers):
            purger=HostPurger(port=client.base_url.port,key_file=key,ca_file=tmp_path/'host-cert.pem')
            assert purger(run)=='purged' and not (runtime/run).exists() and (runtime/other/'runtime.sqlite').exists()
            assert purger(run)=='absent'
            assert purger('not-a-run')=='failed'
            wrong=tmp_path/'wrong-key';wrong.write_text('c'*64);wrong.chmod(0o600)
            assert HostPurger(port=client.base_url.port,key_file=wrong,ca_file=tmp_path/'host-cert.pem')(other)=='failed'
            assert (runtime/other/'runtime.sqlite').exists()
            assert client.post('/internal/v1/runs/purge',headers=headers,json={'run_id':other,'extra':1}).status_code==400
