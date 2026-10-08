"""Actual restricted Backend child. Private input file, fixed redacted outputs."""
import json,sys,time
from pathlib import Path
from nexloop_eios.backend import open_backend

try:
    config=json.loads(Path(sys.argv[1]).read_text())
    with open_backend(database_url=config['database_url'],artifact_root=Path(config['artifact_root']),signing_key_file=Path(config['signing_key_file']),signing_key_id='explicit-configuration') as backend:
        services=backend.authenticate(config['service_token'],world='real')
        print('ready',flush=True)
        deadline=time.monotonic()+10
        while not Path(config['start_file']).exists():
            if time.monotonic()>deadline:raise TimeoutError()
            time.sleep(.005)
        for _ in range(8):
            if config['operation']=='producer':
                receipt=services.prepare_message_context(message_id=config['message_id'],run_token=config['run_token'],command=config['command'],offering_id=config['offering_id'],binding_id=config['binding_id'])
                assert receipt['sha256']==config['sha256'] and receipt['input']==config['input']
            elif config['operation']=='model':
                receipt=services.authorize_runtime_activation(activation_ref=config['activation_ref'],command=config['command'],operation='model')
                assert receipt['authorized'] is True
            else:
                receipt=services.runtime_effect_tool(activation_ref=config['activation_ref'],command=config['command'],tool_operation='submit',parameters={'message':'parallel actual guarded service'})
                assert receipt['receipt']['state']=='accepted'
    print('passed',flush=True)
except Exception:
    print('failed',flush=True)
    sys.exit(1)
