// Web pages batch 6 acceptance (owner walkthrough 2026-10-18, F02/F06/F07/F11) in headless Chromium at 1440×900 against the synthetic
// workbench stack of tests/test_web_page_order_real_browser.py (one firing feed_backlog_age alert seeded by the test). Measures, for the
// owner: facts above the operations on the consumer and alert pages within the first screen; no fillable form without a task (Actions,
// plans); pause/resume with no default scope or operation and its impact said before submit; the dispatch buttons naming their layers
// and the final state never claiming "all open"; the alert in business words with unit, threshold and a link; the read audit's time,
// purpose and member cells on one line with objects as type + short ID. Screenshots and the JSON result go where the test says.
import {readFileSync,writeFileSync} from 'node:fs';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_ORDER_E2E_SESSION_FILE'),'utf8'));
const shots=env('NEXLOOP_ORDER_E2E_SCREENSHOTS');
const result={};
// NEXLOOP_CHROMIUM: an already installed Chromium/headless shell when the pinned Playwright's own build is not downloaded on this machine.
const browser=await chromium.launch({headless:true,...(process.env.NEXLOOP_CHROMIUM?{executablePath:process.env.NEXLOOP_CHROMIUM}:{})});
const top=(page,selector)=>page.evaluate(s=>{const e=document.querySelector(s);return e?Math.round(e.getBoundingClientRect().top+scrollY):null;},selector);
const h2Top=(page,text)=>page.evaluate(t=>{const e=[...document.querySelectorAll('.workbench-page h2')].find(x=>x.textContent.startsWith(t));return e?Math.round(e.getBoundingClientRect().top+scrollY):null;},text);
async function open(page,path,name){await page.goto(path);await page.locator('.workbench-page h1').waitFor({timeout:20000});await page.waitForTimeout(800);
  await page.screenshot({path:`${shots}/${name}.png`,fullPage:true});}
try{
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin,viewport:{width:1440,height:900}});const page=await context.newPage();
  await page.goto('/workbench/overview');await page.locator('#wb-username').waitFor({timeout:20000});
  await page.fill('#wb-username',session.owner.username);await page.fill('#wb-password',readFileSync(session.owner.password_file,'utf8').trim());await page.click('button[type=submit]');
  await page.locator('.workbench-page h1').waitFor({timeout:20000});
  // F02 consumer detail: what this customer is (restriction, properties) inside the first screen, the operations after the facts.
  await open(page,`/workbench/consumers/${session.consumer_id}`,'order-consumer');await page.locator('.workbench-ops').waitFor({timeout:20000});
  result.consumer={facts:await h2Top(page,'属性'),conversations:await h2Top(page,'会话'),ops:await top(page,'.workbench-ops')};
  // F07 alerts: the firing alert first, in business words with unit, threshold and where to go; the silence form after it.
  await open(page,'/workbench/alerts','order-alerts');
  const firing=page.locator('table').first();
  result.alerts={table:await top(page,'table'),ops:await top(page,'.workbench-ops'),text:(await firing.innerText()).replace(/\s+/g,' '),
    link:await firing.locator('a[href="/workbench/operations"]').count()};
  // F02 no task, no form.
  await open(page,'/workbench/actions','order-actions');
  result.actions={summary:await page.locator('.workbench-summary').innerText(),textareas:await page.locator('.workbench-page textarea').count()};
  await open(page,'/workbench/plans','order-plans');
  result.plans={textareas:await page.locator('.workbench-page textarea').count(),empty:await page.locator('.workbench-empty').innerText()};
  // F06 pause/resume: nothing chosen by default; the impact appears once scope and operation are chosen; still nothing submitted.
  await open(page,'/workbench/goals','order-goals');
  result.goals={scope:await page.locator('#control-scope').inputValue(),checked:await page.locator('input[name=control-paused]:checked').count(),
    submit_disabled:await page.locator('.workbench-ops form button[type=submit]').isDisabled(),ops_after_facts:(await h2Top(page,'控制事件'))<(await top(page,'.workbench-ops'))};
  await page.selectOption('#control-scope','tenant');await page.locator('input[name=control-paused]').first().check();
  result.goals.impact=await page.locator('.impact-summary').innerText();
  result.goals.submit_enabled_after=!(await page.locator('.workbench-ops form button[type=submit]').isDisabled());
  await page.screenshot({path:`${shots}/order-goals-impact.png`,fullPage:true});
  // F06 dispatch and F11 read audit (the consumer page read above is audited).
  await open(page,'/workbench/settings','order-settings');
  await page.locator('#workbench-audit-title').waitFor({timeout:20000});await page.locator('section[aria-labelledby="workbench-audit-title"] table').waitFor({timeout:20000});
  result.settings={open_button:await page.getByRole('button',{name:'开启租户层与交付 Action 两层'}).count(),old_button:await page.getByRole('button',{name:'一键开启'}).count(),
    outcome:await page.locator('.rd-outcome').innerText()};
  result.audit=await page.evaluate(()=>{const t=document.querySelector('section[aria-labelledby="workbench-audit-title"] table');
    const one=e=>{const r=e.getBoundingClientRect();const lh=parseFloat(getComputedStyle(e).lineHeight)||parseFloat(getComputedStyle(e).fontSize)*1.6;return r.height<=lh*1.5;};
    return {rows:t.querySelectorAll('tbody tr').length,nowrap_one_line:[...t.querySelectorAll('.nowrap')].every(one),
      ids:[...t.querySelectorAll('.id-ref code')].map(c=>c.textContent),full_in_title:[...t.querySelectorAll('.id-ref-toggle')].every(b=>b.title.length>=b.textContent.length),
      hscroll:document.documentElement.scrollWidth-innerWidth};});
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_ORDER_E2E_RESULT_FILE'),JSON.stringify(result));
