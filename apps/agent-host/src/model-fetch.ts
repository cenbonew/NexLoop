/** 10/18 stage (release 14, Run d659b3b8; owner measurement on APP_HOST): about 3-8 % of connections to the model provider complete
 * TCP within ~10 ms but never finish the TLS handshake. Node's fetch counts the handshake in its 10 s connect timeout, so the first
 * model call failed at 10.49 s however long the request timeout was.
 *
 * The model provider's fetch (pi-ai passes options.fetch to its OpenAI client): its own connector bounds TCP connect plus TLS
 * handshake (default 4 s) and names the phase that stalled; a failure in that phase, before any request byte was written, is
 * retried on a new connection at most `retries` times. Anything after the connection is up (request written, headers, body) is
 * never retried here: the provider may have received and billed it. One fresh Agent per model call, so a retry never reuses a
 * half-open socket of another call. */
import net from 'node:net';
import tls from 'node:tls';
import {Agent,fetch as undiciFetch} from 'undici';

export type ConnectPhase='connect'|'tls';
export type ConnectFailure={phase:ConnectPhase;code:string;attempt:number};
export type ModelConnectOptions={timeoutMs:number;retries:number;ca?:string|Buffer};

const RETRYABLE=new Set(['UND_ERR_CONNECT_TIMEOUT','ECONNREFUSED','EAI_AGAIN']);
const CODE=/^[A-Z][A-Z0-9_]{2,40}$/;

class ConnectPhaseError extends Error{
  constructor(message:string,readonly code:string,readonly phase:ConnectPhase){super(message);}
}

type ConnectOptions={hostname:string;host?:string;protocol:string;port:string;servername?:string};
type Callback=(error:Error|null,socket:net.Socket|tls.TLSSocket|null)=>void;

/** A connector bounding TCP + TLS together; its error carries the phase (connect: no TCP; tls: TCP up, handshake not done). */
export function phasedConnector(timeoutMs:number,ca?:string|Buffer){
  return (options:ConnectOptions,callback:Callback)=>{
    const secure=options.protocol==='https:';
    const port=Number(options.port)||(secure?443:80);
    const host=options.hostname.replace(/^\[|\]$/g,'');
    let tcp=false,done=false;
    const socket:net.Socket=secure
      ?tls.connect({host,port,servername:net.isIP(host)?undefined:(options.servername||host),ALPNProtocols:['http/1.1'],...(ca?{ca}:{})})
      :net.connect({host,port});
    const finish=(error:Error|null)=>{
      if(done)return;done=true;clearTimeout(timer);
      if(error){socket.destroy();callback(error,null);}else callback(null,socket);
    };
    const timer=setTimeout(()=>finish(new ConnectPhaseError(tcp?'model provider TLS handshake timed out':'model provider connect timed out',
      'UND_ERR_CONNECT_TIMEOUT',tcp&&secure?'tls':'connect')),timeoutMs);
    socket.once('connect',()=>{tcp=true;if(!secure)finish(null);});
    if(secure)socket.once('secureConnect',()=>finish(null));
    socket.once('error',(error:NodeJS.ErrnoException)=>finish(new ConnectPhaseError(error.message,typeof error.code==='string'&&CODE.test(error.code)?error.code:'CONNECT_ERROR',
      tcp&&secure?'tls':'connect')));
  };
}

function connectFailure(error:unknown):ConnectPhaseError|undefined{
  for(let current=error,depth=0;current&&typeof current==='object'&&depth<4;current=(current as {cause?:unknown}).cause,depth++)
    if(current instanceof ConnectPhaseError)return current;
  return undefined;
}

/** fetch for one model call; `observe` hears every connection-phase failure (retried or final, codes only) and null once the
 * provider answered (headers received). */
export function modelFetch(options:ModelConnectOptions,observe?:(failure:ConnectFailure|null)=>void):typeof globalThis.fetch{
  return (async(input:Parameters<typeof globalThis.fetch>[0],init?:RequestInit)=>{
    for(let attempt=1;;attempt++){
      const agent=new Agent({connect:phasedConnector(options.timeoutMs,options.ca) as never});
      try{
        const response=await undiciFetch(input as never,{...(init as object),dispatcher:agent} as never);
        observe?.(null);
        return response as unknown as Response;
      }catch(error){
        void agent.close().catch(()=>{});
        const failure=connectFailure(error);
        if(failure)observe?.({phase:failure.phase,code:failure.code,attempt});
        // Only before the request was written: the connector failed, so nothing reached the provider.
        if(!failure||!RETRYABLE.has(failure.code)||attempt>options.retries||init?.signal?.aborted)throw error;
      }
    }
  }) as typeof globalThis.fetch;
}
