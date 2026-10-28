// AT-015 end to end: the mounted WebChat (jsdom) reads a real API over TLS. The EventSource is undici's (HTML-spec reconnect
// with Last-Event-ID), reached through a local TCP relay so the test can cut the TCP connection. Driven by
// tests/test_sse_dom_tcp_resume.py, which starts the API, logs in, commits messages 1-2 and passes the session in a private
// file; this file commits 3-5 itself through the same API and writes what it observed to the result file.
import {readFileSync,writeFileSync} from 'node:fs';
import net from 'node:net';
import {Agent,buildConnector,EventSource as UndiciEventSource,fetch as undiciFetch} from 'undici';
import {JSDOM} from 'jsdom';
import {expect,test} from 'vitest';

// A browser document for React; Event/EventTarget/MessageEvent stay Node's, which undici's EventSource dispatches.
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'https://app.test/'});
for(const name of ['window','document','HTMLElement','HTMLIFrameElement','Node','getComputedStyle'] as const)
  Object.defineProperty(globalThis,name,{configurable:true,writable:true,value:(dom.window as unknown as Record<string,unknown>)[name]});
const {createElement:h,act}=await import('react');
const {createRoot}=await import('react-dom/client');
const {QueryClient,QueryClientProvider}=await import('@tanstack/react-query');
const {WebChat}=await import('../../src/WebChat');

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const env=(name:string)=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};

test('TCP cut, Last-Event-ID resume over TLS: every committed message is displayed once, in order',async()=>{
  const session=JSON.parse(readFileSync(env('NEXLOOP_SSE_E2E_SESSION_FILE'),'utf8')) as {origin:string;port:number;ca_file:string;cookie:string;csrf:string;origin_header:string;conversation_id:string};
  const result=env('NEXLOOP_SSE_E2E_RESULT_FILE');
  const withCookie=(dispatch:Agent['dispatch'])=>(options:Parameters<Agent['dispatch']>[0],handler:Parameters<Agent['dispatch']>[1])=>{
    const headers=options.headers;const added=Array.isArray(headers)?[...headers,'cookie',session.cookie]:{...(headers as Record<string,string>|undefined),cookie:session.cookie};
    return dispatch({...options,headers:added},handler);};
  const ca=readFileSync(session.ca_file);const dispatcher=new Agent({connect:{ca}}).compose(withCookie);

  // TCP relay to the API. `cut()` destroys every relayed connection; while `held`, new connections wait unforwarded.
  const live=new Set<net.Socket>();let held=false;const waiting:net.Socket[]=[];let connections=0;
  const forward=(client:net.Socket)=>{connections++;const upstream=net.connect(session.port,'127.0.0.1');live.add(client);live.add(upstream);
    client.pipe(upstream).pipe(client);for(const s of [client,upstream])s.on('error',()=>{}).on('close',()=>{live.delete(s);client.destroy();upstream.destroy();});};
  const relay=net.createServer(client=>{client.on('error',()=>{});if(held)waiting.push(client);else forward(client);});
  await new Promise<void>(resolve=>relay.listen(0,'127.0.0.1',resolve));
  const relayPort=(relay.address() as net.AddressInfo).port;
  // The stream URL stays the real origin (Host and TLS name as a browser sends them); only its TCP connection goes via the relay.
  const tlsConnect=buildConnector({ca});
  const relayed=new Agent({connect:(options,callback)=>tlsConnect({...options,port:String(relayPort)},callback)}).compose(withCookie);
  const cut=()=>{held=true;for(const s of live)s.destroy();};
  const release=()=>{held=false;for(const s of waiting.splice(0))forward(s);};

  // Events the browser-side EventSource dispatched, per connection (open events delimit connections).
  const received:string[][]=[];const streamStates:number[]=[];
  class RelayedEventSource extends UndiciEventSource{
    constructor(url:string){
      super(session.origin+url,{node:{dispatcher:relayed,reconnectionTime:200}} as ConstructorParameters<typeof UndiciEventSource>[1]);
      this.addEventListener('open',()=>received.push([]));
      this.addEventListener('error',()=>streamStates.push(this.readyState));
      this.addEventListener('committed',event=>received.at(-1)!.push((event as MessageEvent).lastEventId));
    }
  }
  globalThis.EventSource=RelayedEventSource as unknown as typeof EventSource;
  globalThis.fetch=((path:string,init?:RequestInit)=>undiciFetch(session.origin+path,{...(init as object),dispatcher} as Parameters<typeof undiciFetch>[1])) as typeof fetch;

  const commit=async(index:number)=>{
    const response=await undiciFetch(`${session.origin}/api/v1/conversations/${session.conversation_id}/messages`,{method:'POST',dispatcher,
      headers:{'Content-Type':'application/json',Origin:session.origin_header,'X-CSRF-Token':session.csrf,'Idempotency-Key':`dom-tcp-resume-stable-${index}`},
      body:JSON.stringify({body:`committed TCP replay ${index}`})});
    expect(response.status).toBe(202);await response.arrayBuffer();};
  const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
  const shown=()=>[...host.querySelectorAll('ol.messages > li > p')].map(p=>p.textContent);
  const until=async(what:string,check:()=>boolean,ms=15000)=>{const end=Date.now()+ms;
    while(!check()){if(Date.now()>end)throw new Error(`timed out waiting for ${what}; shown ${JSON.stringify(shown())}; connections ${JSON.stringify(received)}; error states ${JSON.stringify(streamStates)}; relayed ${connections}`);await act(async()=>{await new Promise(r=>setTimeout(r,50));});}};
  const expected=(n:number)=>Array.from({length:n},(_,i)=>`committed TCP replay ${i+1}`);
  const same=(n:number)=>()=>JSON.stringify(shown())===JSON.stringify(expected(n));
  const observed:Record<string,unknown>={};
  try{
    await act(async()=>root.render(h(QueryClientProvider,{client:new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}})},h(WebChat))));
    await until('messages 1-2 and an open stream',()=>same(2)()&&received.length===1);
    await commit(3);await until('live message 3',same(3));
    expect(received).toEqual([['3']]);
    cut();await until('the client to notice the cut',()=>host.textContent!.includes('实时连接暂不可用'));
    await commit(4);await commit(5);  // committed while the TCP connection is down
    await new Promise(r=>setTimeout(r,500));expect(shown()).toEqual(expected(3));
    release();await until('messages 4-5 after the resume',same(5));
    // The resumed request carried Last-Event-ID 3: the stream URL still says after=2, yet the second connection starts at 4.
    expect(received).toEqual([['3'],['4','5']]);
    // A refetch of the list after the resume keeps exactly one copy of each row.
    const check=[...host.querySelectorAll('button')].find(b=>b.textContent==='刷新消息')!;
    await act(async()=>check.click());await act(async()=>{await new Promise(r=>setTimeout(r,500));});
    expect(shown()).toEqual(expected(5));expect(new Set(shown()).size).toBe(5);
    Object.assign(observed,{shown:shown(),events_per_connection:received,relay_connections:connections,stream_unavailable_shown:host.textContent!.includes('实时连接暂不可用')});
  }finally{
    await act(async()=>root.unmount());host.remove();relay.close();for(const s of [...live,...waiting])s.destroy();await dispatcher.close();await relayed.close();
    writeFileSync(result,JSON.stringify(observed,null,2)+'\n');
  }
},60000);
