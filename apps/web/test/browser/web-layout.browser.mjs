// Web layout batch 1 acceptance (docs/implementation/web-layout-audit-20261018.md §4.2) in headless Chromium against the synthetic
// workbench stack of tests/test_web_layout_real_browser.py: at 1440, 1024 and 390 px (dark at 1440 and 390), the customer login and
// conversation pages and the workbench login and main pages have no horizontal scroll, no element past the right edge, no text
// under 13 px and one-line table headers; dark screenshots differ from light; below 1024 px the workbench navigation sits behind
// a visible menu button. Batch 2 (same file): on the large synthetic seed, workbench tables become cards below 640 px (no sideways
// scroller, every value labelled, the header row hidden, no card stretched tall) and stay tables above; the customer conversation
// keeps its mode and world in the top bar, names conversations without their raw IDs and pins the composer to the bottom.
// Screenshots go to the directory the test names; the measurements are written as JSON.
import {readFileSync,writeFileSync} from 'node:fs';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_LAYOUT_E2E_SESSION_FILE'),'utf8'));
const shots=env('NEXLOOP_LAYOUT_E2E_SCREENSHOTS');
const secret=file=>readFileSync(file,'utf8').trim();
const SIZES=[[1440,900],[1024,768],[390,844]];const DARK=[[1440,900],[390,844]];
const measure=()=>{const vw=innerWidth;
  const texts=[...document.querySelectorAll('body *')].filter(e=>e.childElementCount===0&&e.innerText&&e.innerText.trim()&&e.getBoundingClientRect().width>0);
  return {hscroll:document.documentElement.scrollWidth-vw,
    // Past the right edge and not inside a horizontal scroller that keeps it reachable (a scroller itself must fit the viewport).
    offscreen:[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();if(!(r.width>0&&r.right>vw+1))return false;
      for(let a=e.parentElement;a;a=a.parentElement){const o=getComputedStyle(a).overflowX;if((o==='auto'||o==='scroll')&&a.getBoundingClientRect().right<=vw+1)return false;}
      return true;}).map(e=>e.tagName+'.'+e.className).slice(0,5),
    font_min:Math.min(...texts.map(e=>parseFloat(getComputedStyle(e).fontSize))),
    th_max:Math.max(0,...[...document.querySelectorAll('th')].map(h=>Math.round(h.getBoundingClientRect().height))),
    // Elements that scroll sideways (content wider than the box), and the shared tables' layout at this width.
    scrollers:[...document.querySelectorAll('body *')].filter(e=>{const o=getComputedStyle(e).overflowX;return (o==='auto'||o==='scroll')&&e.scrollWidth>e.clientWidth+1;})
      .map(e=>e.tagName+'.'+e.className).slice(0,5),
    tables:[...document.querySelectorAll('table.data-table')].map(t=>{const rows=[...t.querySelectorAll('tbody tr')];
      const plain=[...t.querySelectorAll('tbody td:not(.cell-main)')];
      return {rows:rows.length,cards:rows.length>0&&getComputedStyle(rows[0]).display!=='table-row',
        head_hidden:t.querySelector('thead').getBoundingClientRect().height<=1,
        labelled:plain.length===0||plain.every(td=>getComputedStyle(td,'::before').content===JSON.stringify(td.dataset.label)),
        row_max:Math.max(0,...rows.map(r=>Math.round(r.getBoundingClientRect().height)))};}),
    // Batch 3: overview/operations cards — columns in use and whether cards keep their own height; bubbles — widest share of main.
    grid:(g=>g?{cols:new Set([...g.children].map(c=>Math.round(c.getBoundingClientRect().left))).size,
      heights:new Set([...g.children].map(c=>Math.round(c.getBoundingClientRect().height))).size}:null)(document.querySelector('.workbench-grid')),
    bubble_ratio:(m=>m.length?Math.max(...m.map(li=>li.getBoundingClientRect().width/li.parentElement.getBoundingClientRect().width)):null)([...document.querySelectorAll('.workbench-messages li')]),
    empty_styled:[...document.querySelectorAll('.workbench-empty')].filter(e=>!e.closest('.workbench-block')).every(e=>getComputedStyle(e).borderTopStyle==='dashed')};};
const result={pages:{},dark_differs:{},menu:{},chat:{}};
const browser=await chromium.launch({headless:true});
async function shoot(page,name){
  for(const [w,h] of SIZES){await page.setViewportSize({width:w,height:h});await page.emulateMedia({colorScheme:'light'});await page.waitForTimeout(300);
    const buffer=await page.screenshot({path:`${shots}/${name}-${w}.png`,fullPage:true});result.pages[`${name}-${w}`]=await page.evaluate(measure);
    if(DARK.some(([dw])=>dw===w)){await page.emulateMedia({colorScheme:'dark'});await page.waitForTimeout(300);
      const dark=await page.screenshot({path:`${shots}/${name}-${w}-dark.png`,fullPage:true});result.pages[`${name}-${w}-dark`]=await page.evaluate(measure);
      result.dark_differs[`${name}-${w}`]=!dark.equals(buffer);}}
  await page.setViewportSize({width:1440,height:900});await page.emulateMedia({colorScheme:'light'});
}
try{
  let context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin});let page=await context.newPage();
  await page.goto('/');await page.locator('#username').waitFor({timeout:20000});await shoot(page,'customer-login');
  await page.fill('#username',session.customer.username);await page.fill('#password',secret(session.customer.password_file));await page.click('button[type=submit]');
  await page.locator('.chat').waitFor({timeout:20000});await page.locator('.messages li').first().waitFor({timeout:20000});await page.waitForTimeout(800);await shoot(page,'customer-webchat');
  // The conversation picker never shows a raw 64-character ID; mode and world sit in the top bar; on a short phone screen with the
  // message list scrolled to the top, the composer is pinned to the bottom edge of the viewport over the messages.
  result.chat.option_texts_without_ids=await page.locator('#conversation option').evaluateAll(o=>o.length>1&&o.every(x=>!/[0-9a-f]{64}/.test(x.textContent)));
  result.chat.meta_in_bar=await page.locator('.chat-bar .chat-meta').isVisible();
  // A conversation long enough to scroll: four more messages through the composer itself (accepted rows, synthetic text).
  for(let i=1;i<=4;i++){const before=await page.locator('.messages li').count();await page.fill('#chat-message',`版式测试消息 ${i}：合成文本。`);
    await page.locator('.chat-composer button',{hasText:'发送消息'}).click();
    await page.waitForFunction(n=>document.querySelectorAll('.messages li').length>n,before,{timeout:20000});}
  result.chat.sent_through_composer=await page.locator('.messages li').count()>=5;
  await page.setViewportSize({width:390,height:480});await page.evaluate(()=>document.querySelector('.messages').scrollIntoView());await page.waitForTimeout(300);
  result.chat.composer_pinned=await page.evaluate(()=>{const r=document.querySelector('.chat-composer').getBoundingClientRect();
    const list=document.querySelector('.messages').getBoundingClientRect();return list.bottom>r.top&&Math.abs(r.bottom-innerHeight)<=1&&r.height<=innerHeight/2;});
  await page.screenshot({path:`${shots}/customer-webchat-390x480.png`});
  await context.close();
  context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin});page=await context.newPage();
  await page.goto('/workbench/overview');await page.locator('#wb-username').waitFor({timeout:20000});await shoot(page,'wb-login');
  await page.fill('#wb-username',session.owner.username);await page.fill('#wb-password',secret(session.owner.password_file));await page.click('button[type=submit]');
  await page.locator('.workbench-page h1').waitFor({timeout:20000});
  for(const [name,path] of [['wb-overview','/workbench/overview'],['wb-consumers','/workbench/consumers'],['wb-consumer',`/workbench/consumers/${session.consumer_id}`],
    ['wb-conversation',`/workbench/consumers/conversations/${session.conversation_id}`],['wb-settings','/workbench/settings'],
    ['wb-actions','/workbench/actions'],['wb-commitments','/workbench/commitments'],['wb-operations','/workbench/operations'],['wb-goals','/workbench/goals'],['wb-audit','/workbench/audit']]){
    await page.goto(path);await page.locator('.workbench-page h1').waitFor({timeout:20000});await page.waitForTimeout(800);await shoot(page,name);}
  // The menu below 1024 px: a visible button; the navigation hidden until it is opened; a click on an entry navigates and closes it.
  await page.setViewportSize({width:390,height:844});await page.goto('/workbench/overview');await page.locator('.workbench-page h1').waitFor({timeout:20000});
  const toggle=page.locator('.workbench-menu-toggle');
  result.menu.toggle_visible=await toggle.isVisible();result.menu.nav_hidden_before=!(await page.locator('#workbench-nav').isVisible());
  await toggle.click();result.menu.nav_visible_after=await page.locator('#workbench-nav').isVisible();
  result.menu.entries=await page.locator('#workbench-nav a').count();
  await page.locator('#workbench-nav a',{hasText:'消费者'}).click();await page.locator('.workbench-page h1',{hasText:'消费者'}).waitFor({timeout:20000});
  result.menu.closed_after_navigation=!(await page.locator('#workbench-nav').isVisible());
  result.menu.hscroll_open=await (async()=>{await toggle.click();return page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);})();
  await page.screenshot({path:`${shots}/wb-menu-open-390.png`,fullPage:true});
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_LAYOUT_E2E_RESULT_FILE'),JSON.stringify(result));
