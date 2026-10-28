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
    // Batch 3: overview/operations cards — columns in use; UI spec §3 (owner 2026-10-20): the cards of one row are equally tall
    // (rows_equal), the footer at the bottom. Bubbles — widest share of main.
    grid:(g=>{if(!g)return null;const cs=[...g.children].map(c=>c.getBoundingClientRect());const rows=new Map();
      for(const r of cs){const k=Math.round(r.top);rows.set(k,[...(rows.get(k)??[]),Math.round(r.height)]);}
      return {cols:new Set(cs.map(r=>Math.round(r.left))).size,rows_equal:[...rows.values()].every(hs=>Math.max(...hs)-Math.min(...hs)<=1)};})(document.querySelector('.card-grid,.workbench-grid')),
    bubble_ratio:(m=>m.length?Math.max(...m.map(li=>li.getBoundingClientRect().width/li.parentElement.getBoundingClientRect().width)):null)([...document.querySelectorAll('.workbench-messages li')]),
    // Batch 4: every visible radio/checkbox — its box, and the gap to its own text (first text after it inside the label).
    choices:[...document.querySelectorAll('input[type=radio],input[type=checkbox]')].filter(i=>i.getBoundingClientRect().width>0).map(i=>{
      const r=i.getBoundingClientRect();const t=[...i.parentElement.childNodes].find(n=>n.nodeType===3&&n.textContent.trim());
      const range=document.createRange();if(t)range.selectNodeContents(t);const tr=t?range.getBoundingClientRect():null;
      return {w:Math.round(r.width),h:Math.round(r.height),gap:tr?Math.round(tr.left-r.right):null,dy:tr?Math.round(Math.abs((tr.top+tr.bottom)/2-(r.top+r.bottom)/2)):null};}),
    fieldsets:[...document.querySelectorAll('fieldset')].map(f=>({w:Math.round(f.getBoundingClientRect().width),parent:Math.round(f.parentElement.getBoundingClientRect().width)})),
    // Batch 5: breadcrumbs, the time zone note, empty states with a kind and a next step, badges with words, the title's top.
    crumbs:[...document.querySelectorAll('nav.breadcrumbs li')].map(l=>l.textContent.trim()),
    tz:document.querySelector('.brand-tz')?.textContent??null,
    // UI batch 1/3: the earlier Empty (.workbench-empty, data-empty) and the kit's EmptyState (.state, data-state) alike.
    empties:[...document.querySelectorAll('.workbench-empty, .state:not(.state-loading)')].map(e=>({kind:e.dataset.empty??e.dataset.state??null,next:!!(e.querySelector('.empty-next')??e.querySelector('.state-next'))?.textContent.trim()})),
    badges_worded:[...document.querySelectorAll('.status-badge')].every(b=>b.textContent.replace(/[✓✕…!–]/g,'').trim().length>0&&!/^[a-z_]+$/.test(b.textContent.replace(/[✓✕…!–]/g,'').trim())),
    h1_top:(h=>h?Math.round(h.getBoundingClientRect().top):null)(document.querySelector('.workbench-page h1')),
    empty_styled:[...document.querySelectorAll('.workbench-empty')].filter(e=>!e.closest('.workbench-block')).every(e=>getComputedStyle(e).borderTopStyle==='dashed')
      // UI spec §8: an unavailable state (not compact) is dashed, an empty one solid.
      &&[...document.querySelectorAll('.state-unavailable:not(.state-compact)')].every(e=>getComputedStyle(e).borderTopStyle==='dashed')
      &&[...document.querySelectorAll('.state-empty:not(.state-compact)')].every(e=>getComputedStyle(e).borderTopStyle==='solid')};};
const result={pages:{},dark_differs:{},menu:{},chat:{},shell:{},tabs:{},persona:{}};
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
    await page.locator('.chat-composer').getByRole('button',{name:'发送',exact:true}).click();
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
    ['wb-actions','/workbench/actions'],['wb-commitments','/workbench/commitments'],['wb-operations','/workbench/operations'],['wb-goals','/workbench/goals'],['wb-contact','/workbench/contact'],['wb-experiments','/workbench/experiments'],['wb-personas','/workbench/personas'],['wb-audit','/workbench/audit']]){
    await page.goto(path);await page.locator('.workbench-page h1').waitFor({timeout:20000});await page.waitForTimeout(800);await shoot(page,name);}
  // Batch 5 shell at 1440: three navigation groups, the next-stage pages last and marked; tabs on commitments: arrow keys,
  // Home/End and Enter/Space move the selection, aria-selected and the roving tabindex follow, focus stays on the selected tab,
  // and the tab list is the same element (not re-mounted) after the other list loads.
  await page.setViewportSize({width:1440,height:900});await page.goto('/workbench/overview');await page.locator('.workbench-page h1').waitFor({timeout:20000});
  result.shell={groups:await page.locator('#workbench-nav .nav-group-title').allTextContents(),
    last:await page.locator('#workbench-nav a').evaluateAll(a=>a.slice(-2).map(x=>x.textContent))};
  await page.goto('/workbench/commitments');await page.locator('.workbench-toolbar [role=tab]').first().waitFor({timeout:20000});
  const tabState=()=>page.locator('.workbench-toolbar [role=tab]').evaluateAll(t=>t.map(x=>[x.textContent,x.getAttribute('aria-selected'),x.tabIndex,document.activeElement===x]));
  await page.evaluate(()=>{document.querySelector('.workbench-toolbar [role=tablist]').dataset.mark='1';});
  await page.locator('.workbench-toolbar [role=tab][aria-selected=true]').focus();
  const tabs={start:await tabState()};
  await page.keyboard.press('ArrowRight');await page.waitForTimeout(600);tabs.right=await tabState();
  tabs.same_list=await page.evaluate(()=>document.querySelector('.workbench-toolbar [role=tablist]')?.dataset.mark==='1');
  await page.keyboard.press('Home');await page.waitForTimeout(400);tabs.home=await tabState();
  await page.keyboard.press('End');await page.waitForTimeout(400);tabs.end=await tabState();
  await page.keyboard.press('ArrowLeft');await page.keyboard.press('Enter');await page.waitForTimeout(400);tabs.enter=await tabState();
  await page.keyboard.press('ArrowRight');await page.keyboard.press('Space');await page.waitForTimeout(400);tabs.space=await tabState();
  result.tabs=tabs;
  // Operator persona pages: the owner fills a draft and publishes xiaohe@1, publishes rules with it as the default, then the
  // consumer detail shows persona@version and its source (the governed writes through the page's own forms).
  await page.setViewportSize({width:1440,height:900});await page.goto('/workbench/personas');await page.locator('#persona-display_name').waitFor({timeout:20000});
  await page.fill('#persona-persona_id','xiaohe');await page.fill('#persona-display_name','小禾');await page.fill('#persona-tone','亲切、简洁');
  await page.fill('#persona-deflect','我是本店的服务助手，有什么可以帮您？');await page.fill('#persona-reason','首个人设');
  await page.locator('form.persona-editor button[type=submit]').click();
  await page.locator('.data-table code',{hasText:'xiaohe@1'}).waitFor({timeout:20000});
  result.persona={published:await page.locator('.data-table code',{hasText:'xiaohe@1'}).count()};
  await page.locator('[role=tab]',{hasText:'分配'}).click();await page.locator('#rules-default').waitFor({timeout:20000});
  await page.selectOption('#rules-default','xiaohe');await page.fill('#rules-reason','默认人设');
  await page.locator('form[aria-label^="发布分配规则"] button[type=submit]').click();
  await page.getByText('规则版本 v1').waitFor({timeout:20000});
  await page.goto(`/workbench/consumers/${session.consumer_id}`);await page.locator('#consumer-persona-title').waitFor({timeout:20000});
  await page.getByText('下次对话起').waitFor({timeout:20000});
  result.persona.consumer=await page.locator('section[aria-labelledby=consumer-persona-title] dl.facts').innerText();
  await page.screenshot({path:`${shots}/wb-consumer-persona-1440.png`,fullPage:true});
  await page.goto('/workbench/personas');await page.locator('[role=tab]',{hasText:'AI 身份披露'}).click();
  await page.locator('#ai-mode').waitFor({timeout:20000});await page.screenshot({path:`${shots}/wb-personas-disclosure-1440.png`,fullPage:true});
  // The menu below 1024 px: a visible button; the navigation hidden until it is opened; a click on an entry navigates and closes it.
  await page.setViewportSize({width:390,height:844});await page.goto('/workbench/overview');await page.locator('.workbench-page h1').waitFor({timeout:20000});
  const toggle=page.locator('.workbench-menu-toggle');
  result.menu.toggle_visible=await toggle.isVisible();result.menu.nav_hidden_before=!(await page.locator('#workbench-nav').isVisible());
  await toggle.click();result.menu.nav_visible_after=await page.locator('#workbench-nav').isVisible();
  result.menu.entries=await page.locator('#workbench-nav a').count();
  await page.locator('#workbench-nav a',{hasText:'客户'}).click();await page.locator('.workbench-page h1',{hasText:'客户'}).waitFor({timeout:20000});
  result.menu.closed_after_navigation=!(await page.locator('#workbench-nav').isVisible());
  result.menu.hscroll_open=await (async()=>{await toggle.click();return page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);})();
  await page.screenshot({path:`${shots}/wb-menu-open-390.png`,fullPage:true});
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_LAYOUT_E2E_RESULT_FILE'),JSON.stringify(result));
