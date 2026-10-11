// @vitest-environment jsdom
// AT-015 (browser side): the mounted WebChat shows every committed message exactly once, in sequence order, across an
// EventSource disconnect, a Last-Event-ID resume that re-sends rows already on screen, events committed while disconnected,
// the same event arriving twice, and a refetch of the message list that races a live event.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {WebChat} from '../src/WebChat';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const conversationId='c'.repeat(64);
const row=(sequence:number)=>({id:sequence.toString(16).padStart(64,'0'),conversation_id:conversationId,sequence,actor:'consumer-principal',
  body:`committed ${sequence}`,accepted_at:`2026-10-11T00:00:${String(sequence).padStart(2,'0')}Z`,status:'accepted'});

// The committed log the server reads from, and the page the message list endpoint returns.
let committed:ReturnType<typeof row>[]=[];
let listGate:null|{snapshot:ReturnType<typeof row>[];release:()=>void}=null;

// Browser EventSource semantics (HTML spec): the first request uses the URL; after a network error the browser
// reconnects to the same URL and sends the id of the last dispatched event as Last-Event-ID. The server prefers that header.
class FakeEventSource extends EventTarget{
  static CONNECTING=0;static OPEN=1;static CLOSED=2;static all:FakeEventSource[]=[];
  readyState=FakeEventSource.CONNECTING;lastEventId='';onerror:((e:Event)=>void)|null=null;onopen:((e:Event)=>void)|null=null;
  requests:{after:string;lastEventIdHeader:string|null}[]=[];
  constructor(readonly url:string,readonly init?:EventSourceInit){super();FakeEventSource.all.push(this);this.request(null);}
  request(header:string|null){this.requests.push({after:new URL(this.url,'https://app.test').searchParams.get('after')??'0',lastEventIdHeader:header});
    this.readyState=FakeEventSource.OPEN;this.onopen?.(new Event('open'));}
  get cursor(){const last=this.requests.at(-1)!;return Number(last.lastEventIdHeader??last.after);}
  // What the server's stream sends for the current request: committed rows after its cursor (or after `from` when the
  // server re-sends rows the client already has).
  deliver(from=this.cursor){for(const item of committed.filter(r=>r.sequence>from))this.dispatch(item);}
  dispatch(item:ReturnType<typeof row>){if(this.readyState!==FakeEventSource.OPEN)return;this.lastEventId=String(item.sequence);
    this.dispatchEvent(new MessageEvent('committed',{data:JSON.stringify({id:String(item.sequence),type:'message.accepted',data:item}),lastEventId:String(item.sequence)}));}
  dropConnection(){this.readyState=FakeEventSource.CONNECTING;this.onerror?.(new Event('error'));}
  reconnect(){this.request(this.lastEventId||null);}
  serverUnavailable(){this.dispatchEvent(new MessageEvent('unavailable',{data:'{"code":"stream_unavailable"}'}));}
  close(){this.readyState=FakeEventSource.CLOSED;}
}
const open=()=>FakeEventSource.all.filter(s=>s.readyState!==FakeEventSource.CLOSED);
const json=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});

let root:Root;let host:HTMLElement;
beforeEach(()=>{
  committed=[row(1),row(2)];listGate=null;FakeEventSource.all=[];
  vi.stubGlobal('EventSource',FakeEventSource);
  vi.stubGlobal('fetch',vi.fn(async(input:string)=>{
    const path=new URL(input,'https://app.test').pathname;
    if(path==='/api/v1/conversations')return json({items:[{id:conversationId,consumer_id:'d'.repeat(64),world_id:'real',revision:1}],next_cursor:null});
    if(path.endsWith('/handling'))return json({conversation_id:conversationId,handled_by:'agent'});
    if(path.endsWith('/messages')){
      if(listGate){const gate=listGate;await new Promise<void>(resolve=>{gate.release=resolve;});return json({items:gate.snapshot,next_cursor:null});}
      return json({items:[...committed],next_cursor:null});}
    return new Response('{}',{status:404});
  }));
  host=document.createElement('div');document.body.append(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.unstubAllGlobals();vi.useRealTimers();});

const shown=()=>[...host.querySelectorAll('ol.messages > li > p')].map(p=>p.textContent);
const expectExactlyOnce=(count:number)=>expect(shown()).toEqual(Array.from({length:count},(_,i)=>`committed ${i+1}`));
async function settle(){for(let i=0;i<5;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});}
async function mount(){
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  await act(async()=>root.render(h(QueryClientProvider,{client},h(WebChat))));await settle();
  expectExactlyOnce(2);expect(open()).toHaveLength(1);expect(open()[0].requests[0].after).toBe('2');
  return open()[0];
}

test('a Last-Event-ID resume that re-sends displayed rows, plus rows committed while disconnected, shows each row once in order',async()=>{
  const source=await mount();
  committed.push(row(3));await act(async()=>source.deliver());expectExactlyOnce(3);
  await act(async()=>source.dropConnection());
  committed.push(row(4),row(5));  // committed while the client is disconnected
  await act(async()=>source.reconnect());
  expect(source.requests.at(-1)).toEqual({after:'2',lastEventIdHeader:'3'});
  await act(async()=>source.deliver(1));  // the server re-sends 2 and 3, which are already on screen, then 4 and 5
  expectExactlyOnce(5);
  expect(host.textContent).not.toContain('实时连接暂不可用');
});

test('the same committed event delivered twice (race) is displayed once',async()=>{
  const source=await mount();
  committed.push(row(3),row(4));
  await act(async()=>{source.dispatch(row(4));source.dispatch(row(3));source.dispatch(row(4));source.dispatch(row(3));});
  expectExactlyOnce(4);
});

test('a refetch of the list that started before a live event and finishes after it does not remove that event',async()=>{
  const source=await mount();
  listGate={snapshot:[row(1),row(2)],release:()=>{}};  // the GET is served before message 3 commits
  const check=[...host.querySelectorAll('button')].find(b=>b.textContent==='核对已提交消息')!;
  await act(async()=>check.click());
  committed.push(row(3));await act(async()=>source.deliver());expectExactlyOnce(3);
  await act(async()=>listGate!.release());await settle();
  expectExactlyOnce(3);
  // A later refetch that includes the row keeps a single copy.
  listGate=null;await act(async()=>check.click());await settle();expectExactlyOnce(3);
});

test('when the server ends the stream as unavailable, the client resubscribes after the rows on screen and loses nothing',async()=>{
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout']});
  const first=await mountWithFakeTimers();
  await act(async()=>first.serverUnavailable());
  expect(first.readyState).toBe(FakeEventSource.CLOSED);expect(host.textContent).toContain('实时连接暂不可用');
  committed.push(row(3),row(4));
  await act(async()=>{vi.advanceTimersByTime(5000);});
  const second=open();expect(second).toHaveLength(1);expect(second[0]).not.toBe(first);
  expect(second[0].requests[0]).toEqual({after:'2',lastEventIdHeader:null});
  await act(async()=>second[0].deliver());
  expectExactlyOnce(4);expect(host.textContent).not.toContain('实时连接暂不可用');
});

async function mountWithFakeTimers(){
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  await act(async()=>root.render(h(QueryClientProvider,{client},h(WebChat))));
  for(let i=0;i<5;i++)await act(async()=>{await vi.advanceTimersByTimeAsync(0);});
  expectExactlyOnce(2);expect(open()).toHaveLength(1);return open()[0];
}
