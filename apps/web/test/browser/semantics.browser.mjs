// Workbench batch 7 acceptance (owner walkthrough 2026-10-18, F03/F04/F05/F09/F10) in headless Chromium at 1440×900 against the synthetic
// workbench stack of tests/test_web_semantics_real_browser.py (three seeded commitments of one customer, two exceptions, 61 customers).
// Measures, as the owner: a commitment row answers whom, what, when, state and next step without opening it; one name state for one
// customer on every page; list, counts and exceptions follow the selected tab; a customer's late commitments found with filters that
// live in the URL and survive Back with the scroll position; from the customer to their commitments and conversation with breadcrumbs
// naming them; the history panel does not move the conversation being read. Reads only; screenshots and the JSON result go where the
// test says.
import {readFileSync,writeFileSync} from 'node:fs';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_SEMANTICS_E2E_SESSION_FILE'),'utf8'));
const shots=env('NEXLOOP_SEMANTICS_E2E_SCREENSHOTS');
const result={};
const browser=await chromium.launch({headless:true,...(process.env.NEXLOOP_CHROMIUM?{executablePath:process.env.NEXLOOP_CHROMIUM}:{})});
const settle=async(page,name)=>{await page.locator('.workbench-page h1').waitFor({timeout:20000});await page.waitForFunction(()=>!document.querySelector('[aria-busy="true"]'),null,{timeout:20000});
  await page.waitForTimeout(400);if(name)await page.screenshot({path:`${shots}/${name}.png`,fullPage:true});};
const text=(page,selector)=>page.evaluate(s=>[...document.querySelectorAll(s)].map(e=>e.textContent.trim()),selector);
const nameOf=(page,scope)=>page.evaluate(s=>{const e=document.querySelector(s+' .consumer-name');return e?e.textContent.replace(/\s+/g,'').trim():null;},scope);
try{
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin,viewport:{width:1440,height:900}});const page=await context.newPage();
  await page.goto('/workbench/overview');await page.locator('#wb-username').waitFor({timeout:20000});
  await page.fill('#wb-username',session.owner.username);await page.fill('#wb-password',readFileSync(session.owner.password_file,'utf8').trim());await page.click('button[type=submit]');
  await page.locator('.workbench-page h1').waitFor({timeout:20000});

  // F03 + F05 (open tab): the row of the late commitment, the headers, ids secondary; tabs with counts; this scope's exceptions.
  await page.goto('/workbench/commitments');await settle(page,'semantics-commitments-open');
  const rows=async()=>page.evaluate(()=>[...document.querySelectorAll('table.data-table tbody tr')].map(tr=>Object.fromEntries([...tr.querySelectorAll('td')].map(td=>[td.dataset.label,td.textContent.trim()]))));
  const scope=async()=>({tabs:await text(page,'[role=tab]'),selected:(await text(page,'[role=tab][aria-selected=true]'))[0],rows:(await rows()).length,
    scope:(await text(page,'.workbench-page h2')).find(t=>t.startsWith('本范围的异常')),exceptions:(await text(page,'.exceptions-scope li')).map(t=>t.split(' · ')[0]),
    all_exceptions:(await text(page,'.exceptions-all summary'))[0]??null});
  const open=await rows();
  const late=open.find(r=>r['期限'].includes('已逾期'));
  result.f03={late_row:{summary:late?.['承诺'],customer:late?.['客户'],due:late?.['期限'],status:late?.['状态'],next:late?.['下一步']},
    headers:await text(page,'table.data-table thead th'),id_short:open.every(r=>/^[0-9a-f]{12}…/.test(r['ID'])),copy_buttons:await page.locator('.copy-button').count()};
  result.f05={open:await scope()};
  await page.getByRole('tab',{name:/已结束/}).click();await settle(page,'semantics-commitments-ended');
  result.f05.ended=await scope();

  // F04: the same customer's name state on the commitment list, the consumer list and the consumer detail.
  const commitmentName=(await rows())[0]?.['客户']?.replace(/\s+/g,'');
  await page.goto('/workbench/consumers?sort=open_commitments&order=desc');await settle(page,'semantics-consumers');
  const listName=await page.evaluate(id=>{const row=[...document.querySelectorAll('table.data-table tbody tr')].find(tr=>tr.textContent.includes(id.slice(0,12)));
    return row?row.querySelector('.consumer-name').textContent.replace(/\s+/g,''):null;},session.consumer_id);
  result.f09={consumers_total:(await text(page,'.list-total'))[0]};
  await page.goto(`/workbench/consumers/${session.consumer_id}`);await settle(page,'semantics-consumer');
  result.f04={commitments:commitmentName,list:listName,detail:await nameOf(page,'.workbench-lead')};

  // F10: the commitment count on the customer leads to their open commitments, filtered and said so.
  await page.locator('.consumer-header .link-count').click();await settle(page,'semantics-customer-commitments');
  result.f10={commitments_url:new URL(page.url()).pathname+new URL(page.url()).search,chip:(await text(page,'.filter-chip'))[0]?.replace('×','').trim()};

  // F09: narrow to this customer's late commitments; the URL carries it; open one and come Back.
  await page.getByLabel('只看已逾期').check();await settle(page,'semantics-late');
  result.f09.url=new URL(page.url()).search;result.f09.rows=(await rows()).length;result.f09.total=(await text(page,'.list-total'))[0];
  await page.locator('table.data-table tbody tr button.link').first().click();await settle(page);
  await page.goBack();await settle(page);
  result.f09.back_url=new URL(page.url()).search;result.f09.back_rows=(await rows()).length;
  // Scroll position on Back: the consumer list (sorted, 50 rows), scrolled, a customer opened, Back.
  await page.goto('/workbench/consumers?sort=id');await settle(page);
  await page.evaluate(()=>window.scrollTo(0,900));await page.waitForTimeout(300);
  result.f09.scroll_before=await page.evaluate(()=>scrollY);
  await page.locator('table.data-table tbody tr button.link').nth(20).click();await settle(page);
  await page.goBack();await settle(page);await page.waitForTimeout(600);
  result.f09.scroll_back=await page.evaluate(()=>scrollY);
  await page.getByRole('button',{name:'下一页'}).click();await settle(page,'semantics-consumers-page2');
  result.f09.consumers_page2_url=page.url();

  // F10: the conversation names its customer in the breadcrumb; opening the history panel leaves the conversation where it is.
  await page.goto(`/workbench/consumers/${session.consumer_id}`);await settle(page);
  await page.locator('.conversation-item').first().click();await settle(page,'semantics-conversation');
  await page.waitForFunction(()=>document.querySelectorAll('.breadcrumbs li').length>=3,null,{timeout:20000});
  result.f10.crumbs=(await text(page,'.breadcrumbs li')).map(t=>t.replace(/\s+/g,''));
  const currentTop=()=>page.evaluate(()=>{const h=[...document.querySelectorAll('.workbench-page h2')].find(x=>x.textContent==='本会话消息');return Math.round(h.getBoundingClientRect().top);});
  result.f10.current_before=await currentTop();
  const toggle=page.locator('.history-toggle');if(await toggle.count())await toggle.click();await page.waitForTimeout(500);
  result.f10.current_after=await currentTop();
  result.f10.history_snippet=(await text(page,'.history-snippet')).length>0;
  await page.screenshot({path:`${shots}/semantics-conversation-history.png`,fullPage:true});
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_SEMANTICS_E2E_RESULT_FILE'),JSON.stringify(result,null,1));
