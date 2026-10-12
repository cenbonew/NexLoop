// AT-015 in a real browser engine: headless Chromium (Playwright, pinned in apps/web/package.json) runs the built WebChat
// served by the actual API over TLS on 127.0.0.1. Driven by tests/test_sse_real_browser.py, which starts the API with the
// built frontend, prepares the synthetic account and conversation, and restarts the API on request. This script:
//   1. logs in through the login form and waits for committed messages 1-2 with the stream open;
//   2. receives 3 live on the open stream; goes offline (browser network emulation, and every TCP connection of the page
//      cut at the local relay that stands for the network between browser and server); commits 4-5 through the API while
//      offline; comes back online and checks that the page shows 1-5 once each, in order, on the same conversation, without
//      a manual refresh (the browser's EventSource resumes with Last-Event-ID 3);
//   3. reloads the page and checks 1-5 again (restored from the API, same conversation);
//   4. receives 6 live; asks the driver to restart the API; commits 7-8 after the restart; checks 1-8 once each.
// Messages are committed by a second client of the same account (the server produces them while the page is offline).
// Output: the observed list per phase and the Last-Event-ID / after values of every stream request (numbers only).
// Run with Node 24 from apps/web: node test/browser/sse-resume.browser.mjs (environment set by the driver).
import {existsSync,readFileSync,writeFileSync} from 'node:fs';
import net from 'node:net';
import {Agent,request} from 'undici';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_BROWSER_E2E_SESSION_FILE'),'utf8'));
const resultFile=env('NEXLOOP_BROWSER_E2E_RESULT_FILE');
const control=env('NEXLOOP_BROWSER_E2E_CONTROL_DIR');
const password=readFileSync(session.password_file,'utf8').trim();
const dispatcher=new Agent({connect:{ca:readFileSync(session.ca_file)}});

// The page's origin port is this relay; the API listens on its own port behind it. `cut()` destroys every relayed
// connection and holds new ones until `release()`, as a dropped network would.
const live=new Set();let held=false;const waiting=[];let relayConnections=0;
const forward=client=>{relayConnections++;const upstream=net.connect(session.api_port,'127.0.0.1');live.add(client);live.add(upstream);
  client.pipe(upstream).pipe(client);for(const s of [client,upstream])s.on('error',()=>{}).on('close',()=>{live.delete(s);client.destroy();upstream.destroy();});};
const relay=net.createServer(client=>{client.on('error',()=>{});if(held)waiting.push(client);else forward(client);});
await new Promise(resolve=>relay.listen(session.origin_port,'127.0.0.1',resolve));
const cut=()=>{held=true;for(const s of live)s.destroy();};
const release=()=>{held=false;for(const s of waiting.splice(0))forward(s);};

// Commits go straight to the API (another client of the same account), not through the page's network.
const commit=async index=>{
  const response=await request(`https://127.0.0.1:${session.api_port}/api/v1/conversations/${session.conversation_id}/messages`,{method:'POST',dispatcher,
    headers:{'content-type':'application/json',host:`127.0.0.1:${session.origin_port}`,origin:session.origin,cookie:session.cookie,'x-csrf-token':session.csrf,'idempotency-key':`browser-resume-stable-${index}`},
    body:JSON.stringify({body:`browser resume ${index}`})});
  await response.body.arrayBuffer();if(response.statusCode!==202)throw new Error(`commit ${index} returned ${response.statusCode}`);};
const expected=n=>Array.from({length:n},(_,i)=>`browser resume ${i+1}`);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

const observed={phases:{},stream_requests:[]};
const browser=await chromium.launch({headless:true});
observed.browser_version=browser.version();
try{
  // The API's certificate is the test CA of this run, for 127.0.0.1 only.
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin});
  const page=await context.newPage();
  // allHeaders(): the headers Chromium actually sent (the provisional ones lack Last-Event-ID).
  const pendingHeaders=[];
  page.on('request',request=>{
    const url=new URL(request.url());
    if(!url.pathname.endsWith('/events'))return;
    const entry={after:Number(url.searchParams.get('after')),last_event_id:null};observed.stream_requests.push(entry);
    pendingHeaders.push(request.allHeaders().then(h=>{entry.last_event_id=h['last-event-id']??null;},()=>{}));});
  const shown=()=>page.locator('ol.messages > li > p').allTextContents();
  const until=async(what,check,ms=20000)=>{const end=Date.now()+ms;
    for(;;){const value=await shown();if(await check(value))return value;if(Date.now()>end)throw new Error(`timed out waiting for ${what}; shown ${JSON.stringify(value)}`);await sleep(100);}};
  const same=n=>value=>JSON.stringify(value)===JSON.stringify(expected(n));
  const selected=()=>page.locator('select#conversation').inputValue();

  await page.goto('/');
  await page.fill('#username',session.username);await page.fill('#password',password);await page.click('button[type=submit]');
  await until('messages 1-2',same(2));
  await sleep(500);  // the stream subscribes after the list is read
  observed.phases.initial={shown:await shown(),conversation_matches:(await selected())===session.conversation_id};

  // A live event on the open stream first, so the browser has a last event id to resume from.
  await commit(3);await until('live message 3',same(3));
  // Offline: the open stream drops; the page notices; the server commits 4-5 meanwhile.
  await context.setOffline(true);cut();
  await page.getByText('实时连接暂不可用').waitFor({timeout:20000});
  await commit(4);await commit(5);
  await sleep(1000);
  const whileOffline=await shown();
  release();await context.setOffline(false);const onlineAt=Date.now();
  const resumed=await until('messages 4-5 after the network returns',same(5),30000);const shownAfterMs=Date.now()-onlineAt;
  await sleep(1000);  // a late duplicate would show up here
  observed.phases.offline={shown_while_offline:whileOffline,shown_after:await shown(),distinct:new Set(resumed).size,
    conversation_matches:(await selected())===session.conversation_id,
    unavailable_cleared:await page.getByText('实时连接暂不可用').waitFor({state:'hidden',timeout:10000}).then(()=>true,()=>false),
    unavailable_cleared_after_ms:Date.now()-onlineAt,
    messages_shown_after_ms:shownAfterMs,
    unavailable_count:await page.getByText('实时连接暂不可用').count()};

  // Reload: the page restores the same conversation and messages from the API.
  await page.reload();
  await until('messages 1-5 after reload',same(5));
  await sleep(500);
  observed.phases.reload={shown:await shown(),conversation_matches:(await selected())===session.conversation_id};

  // Server restart: the driver stops and restarts the API on the same port; the stream resumes from where it was.
  await commit(6);await until('live message 6',same(6));
  writeFileSync(`${control}/restart-request`,'1');
  for(const end=Date.now()+60000;!existsSync(`${control}/restarted`);){if(Date.now()>end)throw new Error('API restart timed out');await sleep(100);}
  await commit(7);await commit(8);
  await until('messages 7-8 after the API restart',same(8),40000);
  await sleep(1000);
  observed.phases.restart={shown:await shown(),distinct:new Set(await shown()).size,conversation_matches:(await selected())===session.conversation_id};
  await Promise.all(pendingHeaders);
  await context.close();
}finally{
  await browser.close();await dispatcher.close();relay.close();for(const s of [...live,...waiting])s.destroy();
  observed.relay_connections=relayConnections;
  writeFileSync(resultFile,JSON.stringify(observed,null,2)+'\n');
}
