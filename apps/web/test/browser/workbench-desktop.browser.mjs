// Workbench on desktop and phone (owner feedback 2026-10-17), in headless Chromium (Playwright, pinned in apps/web/package.json) against
// the synthetic workbench stack of tests/test_workbench_desktop_real_browser.py: layout at 1440 px (wide content, one-line table
// headers), the customer's readable name or alias, conversations newest first with "最新", the takeover bar at the top of a
// conversation with a visible result after take-over and hand-back, and no horizontal overflow at 375 px. Screenshots go to the
// directory the test names; the result is written as JSON. Synthetic accounts only.
import {readFileSync,writeFileSync} from 'node:fs';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_WORKBENCH_E2E_SESSION_FILE'),'utf8'));
const shots=env('NEXLOOP_WORKBENCH_E2E_SCREENSHOTS');
const password=readFileSync(session.password_file,'utf8').trim();
const result={};
const browser=await chromium.launch({headless:true});
try{
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin,viewport:{width:1440,height:900}});  // the run's own loopback certificate
  const page=await context.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.goto('/workbench/consumers');
  await page.fill('#wb-username',session.username);await page.fill('#wb-password',password);await page.click('button[type=submit]');
  await page.locator('table.workbench-table tbody tr').first().waitFor({timeout:20000});
  result.desktop={
    main:await page.locator('main').evaluate(e=>Math.round(e.getBoundingClientRect().width)),
    nav:await page.locator('.workbench-layout nav').evaluate(e=>Math.round(e.getBoundingClientRect().width)),
    header_heights:await page.locator('th').evaluateAll(xs=>xs.map(x=>Math.round(x.getBoundingClientRect().height))),
    name:await page.locator('.consumer-name').first().innerText(),
  };
  await page.screenshot({path:`${shots}/consumers-1440.png`,fullPage:true});
  await page.locator('tbody .link').first().click();
  await page.locator('.conversation-item').first().waitFor({timeout:20000});
  result.detail={lead:await page.locator('.workbench-lead').innerText(),items:await page.locator('.conversation-item').allInnerTexts()};
  await page.screenshot({path:`${shots}/consumer-1440.png`,fullPage:true});
  await page.locator('.conversation-item').first().click();
  await page.locator('.takeover-bar').waitFor({timeout:20000});
  const top=async sel=>page.locator(sel).first().evaluate(e=>e.getBoundingClientRect().top);
  result.conversation={bar_above_messages:(await top('.takeover-bar'))<(await top('.workbench-messages')),pill_before:await page.locator('.takeover-pill').innerText()};
  await page.fill('#takeover-reason','合成测试：人工核对');await page.getByRole('button',{name:'接管本会话'}).click();
  try{await page.locator('.takeover-notice',{hasText:'已接管'}).waitFor({timeout:20000});}
  catch(e){await page.screenshot({path:`${shots}/failure.png`,fullPage:true});throw new Error('take-over not confirmed: '+(await page.locator('.takeover-bar').innerText()));}
  result.conversation.pill_taken=await page.locator('.takeover-pill').innerText();
  await page.screenshot({path:`${shots}/conversation-taken-1440.png`,fullPage:true});
  await page.fill('#handback-reason','合成测试：处理完毕');await page.getByRole('button',{name:'交还给 Agent'}).click();
  await page.locator('.takeover-notice',{hasText:'已交还给 Agent'}).waitFor({timeout:20000});
  result.conversation.pill_back=await page.locator('.takeover-pill').innerText();
  result.conversation.notice=await page.locator('.takeover-notice').innerText();
  await page.screenshot({path:`${shots}/conversation-handed-back-1440.png`,fullPage:true});
  // Real dispatch switches (owner 2026-10-18): the owner's one-click open and close through the governed Action, state read back.
  await page.goto('/workbench/settings');
  await page.locator('.real-dispatch-layers').waitFor({timeout:20000});
  const pills=async()=>page.locator('.real-dispatch-layers tbody tr').evaluateAll(rows=>rows.map(r=>[r.cells[0].innerText,r.querySelector('.rd-pill').innerText]));
  result.switches={before:await pills()};
  await page.getByRole('button',{name:'一键开启'}).click();
  result.switches.plan=await page.locator('.rd-confirm ol').innerText();
  await page.fill('#rd-reason','合成测试：负责人打开真实派发');await page.getByRole('button',{name:'确认执行'}).click();
  await page.locator('.rd-results').waitFor({timeout:20000});await page.waitForTimeout(500);
  result.switches.opened=await page.locator('.rd-results').innerText();result.switches.after_open=await pills();
  await page.screenshot({path:`${shots}/real-dispatch-opened-1440.png`,fullPage:true});
  await page.getByRole('button',{name:'一键关闭'}).click();
  await page.fill('#rd-reason','合成测试：演示结束关闭');await page.getByRole('button',{name:'确认执行'}).click();
  await page.locator('.rd-results',{hasText:'关闭'}).waitFor({timeout:20000});await page.waitForTimeout(500);
  result.switches.closed=await page.locator('.rd-results').innerText();result.switches.after_close=await pills();
  await page.setViewportSize({width:375,height:812});
  const overflow={};
  for(const [name,path] of [['consumers','/workbench/consumers'],['consumer',`/workbench/consumers/${session.consumer_id}`],['conversation',`/workbench/consumers/conversations/${session.conversation_id}`]]){
    await page.goto(path);await page.locator('.workbench-page h1').waitFor({timeout:20000});await page.waitForTimeout(500);
    overflow[name]=await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth);
    await page.screenshot({path:`${shots}/${name}-375.png`,fullPage:true});
  }
  result.phone={overflow};result.errors=errors;
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_WORKBENCH_E2E_RESULT_FILE'),JSON.stringify(result));
