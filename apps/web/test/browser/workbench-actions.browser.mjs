// Web layout batch 8: the workbench Action page on real intents (tests/test_workbench_actions_badges_real_browser.py): each row's
// status badge (words, tone, the technical code only in the title), the dispatch column and the observation time element.
import {readFileSync,writeFileSync} from 'node:fs';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_ACTIONS_E2E_SESSION_FILE'),'utf8'));
const shots=env('NEXLOOP_ACTIONS_E2E_SCREENSHOTS');
const browser=await chromium.launch({headless:true});
const result={};
try{
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin,viewport:{width:1440,height:900}});const page=await context.newPage();
  await page.goto('/workbench/actions');await page.locator('#wb-username').waitFor({timeout:20000});
  await page.fill('#wb-username',session.owner.username);await page.fill('#wb-password',readFileSync(session.owner.password_file,'utf8').trim());
  await page.click('button[type=submit]');await page.locator('.data-table tbody tr').first().waitFor({timeout:20000});
  result.rows=await page.locator('.data-table tbody tr').evaluateAll(rows=>rows.map(r=>{const b=r.querySelector('.status-badge');const cell=l=>r.querySelector(`td[data-label="${l}"]`);
    const t=cell('最近观察')?.querySelector('time');
    return {text:b?.textContent.replace(/[✓✕…!–]/g,'').trim()??null,tone:[...(b?.classList??[])].find(c=>c.startsWith('tone-'))??null,code:b?.getAttribute('title')??null,
      dispatch:cell('派发预判')?.textContent.trim()??null,time:t?{datetime:t.getAttribute('datetime'),text:t.textContent,title:t.getAttribute('title')}:null};}));
  // The live region: after a filter switch the screen reader hears the new result count; the filter keeps focus.
  const live=()=>page.locator('[data-announce]').textContent();
  result.live={all:await live()};
  await page.locator('#intent-state').focus();await page.selectOption('#intent-state','failed');
  await page.waitForFunction(()=>document.querySelector('[data-announce]')?.textContent.startsWith('失败'),null,{timeout:20000});
  result.live.failed=await live();result.live.focus_kept=await page.evaluate(()=>document.activeElement?.id==='intent-state');
  result.live.attrs=await page.locator('[data-announce]').evaluate(e=>[e.getAttribute('aria-live'),e.getAttribute('role'),e.getBoundingClientRect().width<=1]);
  await page.selectOption('#intent-state','');await page.waitForFunction(()=>document.querySelector('[data-announce]')?.textContent.startsWith('全部'),null,{timeout:20000});
  for(const [w,h,dark] of [[1440,900,false],[390,844,false],[1440,900,true]]){await page.setViewportSize({width:w,height:h});await page.emulateMedia({colorScheme:dark?'dark':'light'});
    await page.waitForTimeout(300);await page.screenshot({path:`${shots}/wb-actions-${w}${dark?'-dark':''}.png`,fullPage:true});}
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_ACTIONS_E2E_RESULT_FILE'),JSON.stringify(result));
