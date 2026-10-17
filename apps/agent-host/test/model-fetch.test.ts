/** 10/18 (release 14, owner measurement on APP_HOST): some model provider connections complete TCP but never the TLS handshake.
 * The model fetch bounds TCP + TLS, names the phase, and retries only that phase on a new connection; once a request was
 * written nothing is retried. A local server: the first connection is accepted and left silent, later ones are real HTTPS. */
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import https from 'node:https';
import net from 'node:net';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {afterEach,describe,expect,it} from 'vitest';
import {modelFetch,type ConnectFailure} from '../src/model-fetch.ts';
import {selectTrustedModel} from '../src/trusted-model-profile.ts';

const directory=mkdtempSync(join(tmpdir(),'nexloop-model-fetch-'));
execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1',
  '-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
const key=readFileSync(join(directory,'key')),cert=readFileSync(join(directory,'cert'));
const closers:(()=>void)[]=[];
afterEach(()=>{for(const close of closers.splice(0))close();});
process.on('exit',()=>rmSync(directory,{recursive:true,force:true}));

/** silent: how many first connections stay TCP-only (never answer TLS); onRequest: the HTTPS handler. */
async function server(silent:number,onRequest:(request:import('node:http').IncomingMessage,response:import('node:http').ServerResponse)=>void){
  const secure=https.createServer({key,cert},onRequest);
  const held:net.Socket[]=[];let connections=0;
  const front=net.createServer(socket=>{connections++;if(connections<=silent){held.push(socket);socket.on('error',()=>{});return;}secure.emit('connection',socket);});
  await new Promise<void>(resolve=>front.listen(0,'127.0.0.1',resolve));
  closers.push(()=>{for(const s of held)s.destroy();front.close();secure.close();});
  return {url:`https://127.0.0.1:${(front.address() as net.AddressInfo).port}/v1/models`,connections:()=>connections};
}

describe('the model provider connection',()=>{
  it('a TLS handshake that never completes is retried on a new connection after about 4 s; the second one answers',async()=>{
    const seen:(ConnectFailure|null)[]=[];let requests=0;
    const s=await server(1,(_,response)=>{requests++;response.writeHead(401,{'content-type':'application/json'});response.end('{"error":"unauthorized"}');});
    const began=Date.now();
    const response=await modelFetch({timeoutMs:4000,retries:2,ca:cert},failure=>seen.push(failure))(s.url,{method:'GET'});
    const elapsed=Date.now()-began;
    expect(response.status).toBe(401);
    expect(seen).toEqual([{phase:'tls',code:'UND_ERR_CONNECT_TIMEOUT',attempt:1},null]);
    expect(elapsed).toBeGreaterThanOrEqual(3900);expect(elapsed).toBeLessThan(8000);
    expect(s.connections()).toBe(2);expect(requests).toBe(1);
  },20000);

  it('stops after the bounded retries: three silent connections, the last failure names the tls phase',async()=>{
    const seen:(ConnectFailure|null)[]=[];
    const s=await server(3,(_,response)=>{response.end('{}');});
    await expect(modelFetch({timeoutMs:300,retries:2,ca:cert},failure=>seen.push(failure))(s.url)).rejects.toThrow();
    expect(seen.map(f=>f&&[f.phase,f.attempt])).toEqual([['tls',1],['tls',2],['tls',3]]);
    expect(s.connections()).toBe(3);
  });

  it('no TCP at all is the connect phase (refused), retried the same way',async()=>{
    const probe=net.createServer();await new Promise<void>(resolve=>probe.listen(0,'127.0.0.1',resolve));
    const port=(probe.address() as net.AddressInfo).port;await new Promise(resolve=>probe.close(resolve));
    const seen:(ConnectFailure|null)[]=[];
    await expect(modelFetch({timeoutMs:300,retries:2,ca:cert},failure=>seen.push(failure))(`https://127.0.0.1:${port}/v1/models`)).rejects.toThrow();
    expect(seen.map(f=>f&&[f.phase,f.code])).toEqual([['connect','ECONNREFUSED'],['connect','ECONNREFUSED'],['connect','ECONNREFUSED']]);
  });

  it('a failure after the request was written is never retried (the provider may have received and billed it)',async()=>{
    const seen:(ConnectFailure|null)[]=[];let requests=0;
    const s=await server(0,(request)=>{requests++;request.socket.destroy();});
    await expect(modelFetch({timeoutMs:2000,retries:2,ca:cert},failure=>seen.push(failure))(s.url,{method:'POST',body:'{"model":"x"}',
      headers:{'content-type':'application/json'}})).rejects.toThrow();
    expect(requests).toBe(1);expect(s.connections()).toBe(1);expect(seen).toEqual([]);
  });

  it('a caller abort is not retried',async()=>{
    const s=await server(3,(_,response)=>{response.end('{}');});
    const controller=new AbortController();setTimeout(()=>controller.abort(),100);
    const seen:(ConnectFailure|null)[]=[];
    await expect(modelFetch({timeoutMs:2000,retries:2,ca:cert},failure=>seen.push(failure))(s.url,{signal:controller.signal})).rejects.toThrow();
    expect(s.connections()).toBe(1);
  });

  it('through the actual DeepSeek SDK path (trusted profile): a stalled handshake is retried and the provider answers once',async()=>{
    let requests=0;const seen:(ConnectFailure|null)[]=[];
    const s=await server(1,(request,response)=>{requests++;request.resume();response.writeHead(401,{'content-type':'application/json'});
      response.end('{"error":{"message":"synthetic unauthorized","type":"authentication_error"}}');});
    const selection=selectTrustedModel({MODEL_PROVIDER:'deepseek',MODEL_ID:'deepseek-flash',MODEL_API_KEY:'offline-synthetic-key-never-a-real-credential',
      MODEL_BASE_URL:'https://api.deepseek.com'},()=>{throw Error('no file reads allowed');},false);
    const model=selection.models.getModel('deepseek','deepseek-flash')!;
    const local=modelFetch({timeoutMs:500,retries:2,ca:cert},failure=>seen.push(failure));
    // The SDK builds the real request; only its destination is this local server.
    const fetch=((input:string|URL|Request,init?:RequestInit)=>local(s.url.replace('/v1/models',new URL(String(input instanceof Request?input.url:input)).pathname),init)) as typeof globalThis.fetch;
    const result=await selection.models.streamSimple(model,{messages:[{role:'user',content:'synthetic',timestamp:1}]},{maxRetries:0,timeoutMs:5000,fetch} as never).result();
    expect(result.stopReason).toBe('error');expect(String(result.errorMessage)).toContain('401');
    expect(seen).toEqual([{phase:'tls',code:'UND_ERR_CONNECT_TIMEOUT',attempt:1},null]);
    expect(requests).toBe(1);expect(s.connections()).toBe(2);
  });
});
