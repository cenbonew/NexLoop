// Measurement-only Node preload for the Agent Host (NX-049 profile).
// Injected as `--import` by scripts/perf/hooks/sitecustomize.py only inside a measuring
// test process tree (NEXLOOP_PERF_TIMELINE=1). Records, per https request the Host makes
// (guard authorize, effect tools) and per request it serves (runs/start|inspect), wall
// timestamps and phase durations to <NEXLOOP_PERF_OUT>/host-<pid>.jsonl. Paths, status
// codes and timings only: no header, body, key or credential is ever recorded.
import https from 'node:https';
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';

const out=process.env.NEXLOOP_PERF_OUT;
const file=out?`${out}/host-${process.pid}.jsonl`:null;
const write=record=>{if(file)try{fs.appendFileSync(file,JSON.stringify(record)+'\n');}catch{}};
const now=()=>performance.timeOrigin+performance.now();
write({kind:'process',pid:process.pid,origin_wall_ms:performance.timeOrigin,preload_wall_ms:now(),node:process.version});

const pathOf=args=>{
  for(const value of args){
    if(value instanceof URL)return value.pathname;
    if(typeof value==='string'){try{return new URL(value).pathname;}catch{return value;}}
    if(value&&typeof value==='object'&&typeof value.path==='string')return value.path.split('?')[0];
  }
  return '?';
};

const request=https.request;
https.request=function(...args){
  const start=now(),record={kind:'client',pid:process.pid,path:pathOf(args),start_wall_ms:start,phases:{}};
  let done=false;
  const finish=extra=>{if(done)return;done=true;Object.assign(record,extra,{total_ms:now()-start});write(record);};
  const req=request.apply(this,args);
  req.on('socket',socket=>{
    record.phases.socket_ms=now()-start;
    socket.once('connect',()=>{record.phases.tcp_connect_ms=now()-start;});
    socket.once('secureConnect',()=>{record.phases.tls_handshake_done_ms=now()-start;});
  });
  req.once('finish',()=>{record.phases.request_sent_ms=now()-start;});
  req.once('response',response=>{
    record.phases.response_headers_ms=now()-start;record.status=response.statusCode;
    response.once('end',()=>finish({}));
    response.once('error',error=>finish({error:error.code||error.name}));
  });
  req.once('timeout',()=>{record.phases.timeout_ms=now()-start;});
  req.once('error',error=>finish({error:error.code||error.name||'error'}));
  req.once('close',()=>finish({closed_without_response:record.status===undefined}));
  return req;
};

const createServer=https.createServer;
https.createServer=function(...args){
  const server=createServer.apply(this,args);
  server.once('listening',()=>write({kind:'listening',pid:process.pid,wall_ms:now(),since_process_start_ms:now()-performance.timeOrigin}));
  server.on('request',(req,res)=>{
    const start=now(),path=(req.url||'?').split('?')[0];
    res.once('finish',()=>write({kind:'server',pid:process.pid,path,start_wall_ms:start,total_ms:now()-start,status:res.statusCode}));
    res.once('close',()=>{if(!res.writableFinished)write({kind:'server',pid:process.pid,path,start_wall_ms:start,total_ms:now()-start,status:'closed'});});
  });
  return server;
};
syncBuiltinESMExports();
