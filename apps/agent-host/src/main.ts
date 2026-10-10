/** Internal Host; optional backend-governed runtime admission, no DB credentials. */
import {type IncomingMessage, type ServerResponse} from 'node:http';
import {createServer} from 'node:https';
import {closeSync, fstatSync, lstatSync} from 'node:fs';
import {readPrivateMaterial} from './private-material.js';
import {join, resolve} from 'node:path';
import {randomUUID, timingSafeEqual} from 'node:crypto';
import {admissionMetrics} from './run-admission-gate.js';

async function serve() {
  if (process.versions.node.split('.')[0] !== '24') throw new Error('Node 24 required');
  const [runtimeRoot, descriptor, keyPath, portText, certificatePath, tlsKeyPath, runtimeConfigPath] = process.argv.slice(2);
  if (!runtimeRoot || !descriptor || !keyPath || !portText || !certificatePath || !tlsKeyPath) throw new Error('configuration required');
  const fd = Number(descriptor), port = Number(portText);
  if (!Number.isInteger(fd) || fd < 3 || !Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('configuration refused');
  const root = resolve(runtimeRoot), ownerPath = join(root, '.nexloop-owner.lock');
  function ownerHealthy() {
    const directory = lstatSync(root), held = fstatSync(fd), current = lstatSync(ownerPath);
    return directory.isDirectory() && directory.uid === process.getuid!() && (directory.mode & 0o777) === 0o700
      && held.isFile() && current.isFile() && held.uid === process.getuid!() && held.nlink === 1
      && (held.mode & 0o777) === 0o600 && current.dev === held.dev && current.ino === held.ino;
  }
  const privateMaterial=readPrivateMaterial;
  function readKey() {
    const value = privateMaterial(keyPath!,64).toString('utf8');
    if (!/^[0-9a-f]{64}$/.test(value)) throw new Error('internal key refused');
    return Buffer.from(value, 'hex');
  }
  if (!ownerHealthy()) throw new Error('owner unavailable');
  readKey();
  function respond(response:ServerResponse, status:number, content:unknown) {
    response.writeHead(status, {'Content-Type':'application/json', 'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'});
    response.end(JSON.stringify(content));
  }
  function error(response:ServerResponse, status:number) {
    respond(response,status,{code:status===401?'unauthenticated':status===503?'dependency_unavailable':'not_found',message:'Resource unavailable',trace_id:randomUUID(),retryable:status===503,details:{}});
  }
  const runtimeModule=runtimeConfigPath?await import('./runtime-host.js'):undefined;
  const runtime=runtimeModule?new runtimeModule.RuntimeHost(root,runtimeConfigPath!,privateMaterial,()=>{if(!ownerHealthy())throw new Error('owner unavailable');}):undefined;
  const server = createServer({cert:privateMaterial(certificatePath,32768),key:privateMaterial(tlsKeyPath,32768),minVersion:'TLSv1.2'}, (request:IncomingMessage,response:ServerResponse) => {
    void (async()=>{
    // This foundation listens only on loopback. It accepts no proxy identity,
    // browser cookies or cross-origin request, and logs no request headers.
    if (request.headers.host !== `127.0.0.1:${port}` || request.headers.origin !== undefined
        || request.headers['sec-fetch-site'] !== undefined || request.headers.cookie !== undefined) { error(response,401); return; }
    if (request.method==='GET' && request.url==='/health/live') { respond(response,200,{alive:true}); return; }
    const runtimeOperation=runtime&&request.method==='POST'?/^\/internal\/v1\/runs\/(start|resume|inspect|cancel)$/.exec(request.url??'')?.[1]:undefined;
    const metrics=request.method==='GET' && request.url==='/internal/v1/metrics';
    if (!runtimeOperation && !metrics && (request.method!=='GET' || request.url!=='/internal/v1/health/ready')) {request.resume();error(response,404);return;}
    try {
      if (!ownerHealthy()) { error(response,503);return; }
      const key=readKey(), header=request.headers.authorization;
      if (request.headersDistinct.authorization?.length!==1 || !header || !/^Bearer [0-9a-f]{64}$/.test(header)
          || !timingSafeEqual(key,Buffer.from(header.slice(7),'hex'))) {error(response,401);return;}
      // NX-030 M09: concurrency counters only (numbers), same loopback + key as every internal route.
      if(metrics){respond(response,200,{host:runtime?admissionMetrics():{available:false}});return;}
      if(runtimeOperation){
        try{const result=await runtime!.dispatch(runtimeOperation,await runtimeModule!.readRuntimeBody(request));respond(response,runtimeOperation==='start'||runtimeOperation==='resume'?202:200,result);}
        catch(cause){const code=(cause as {code?:string}).code??'dependency_unavailable';const status=code==='runtime_request_conflict'?409:code==='runtime_request_too_large'?413:code.startsWith('invalid_')?400:503;respond(response,status,{code,message:'Resource unavailable',trace_id:randomUUID(),retryable:status===503,details:{}});}
        return;
      }
      // Authentication is control-plane identity only. Run-bound service/agent
      // credentials and PG leases must be integrated before admission exists.
      respond(response,503,{ready:false,product_ready:false,foundation:{owner_lock:true,internal_auth:true},
        missing_capabilities:runtime?['business_action_gateway','channel_dispatch']:['run_admission','run_bound_identity','runtime_adapter','pg_run_lease']});
    } catch {error(response,503);}
    })().catch(()=>{if(!response.headersSent)error(response,503);else response.destroy();});
  });
  server.requestTimeout=5000;server.headersTimeout=5000;server.keepAliveTimeout=1000;server.maxHeadersCount=24;
  server.on('clientError',(_error,socket)=>socket.destroy());
  server.on('error',()=>{process.stderr.write('Agent Host listener unavailable\n');process.exit(1);});
  server.listen(port,'127.0.0.1');
  let closing=false;
  const shutdown=()=>{
    if (closing) return;closing=true;
    server.close(()=>{void (async()=>{try{await runtime?.close();closeSync(fd);process.exit(0);}catch{process.exit(1);}})();});
    server.closeAllConnections();
  };
  process.on('SIGTERM',shutdown);process.on('SIGINT',shutdown);
}
void serve().catch(()=>{process.stderr.write('Agent Host configuration unavailable\n');process.exit(1);});
