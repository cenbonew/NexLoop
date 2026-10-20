// Enterprise world model W1 acceptance in headless Chromium against the synthetic workbench stack of
// tests/test_world_model_real_browser.py (one synthetic type with a restricted property and a relation, seeded by the test). As the
// owner at 1440×900 and 390×844: 企业世界模型 as an expandable entry of the "模型与资产" group with one page per concept (EIOS order),
// the summary counts, the Property page with the restricted property by name only, the Object Type page's Table / Graph switch and
// the graph driven by keyboard only (the switch with Tab and Enter, a node with Tab and Enter, the panel), fitted with legible labels,
// the URL carrying section/view/focus, Back walking it, no horizontal page scroll. Screenshots and the JSON result go where the test says.
import {readFileSync,writeFileSync} from 'node:fs';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_WORLD_E2E_SESSION_FILE'),'utf8'));
const shots=env('NEXLOOP_WORLD_E2E_SCREENSHOTS');
const result={};
const browser=await chromium.launch({headless:true,...(process.env.NEXLOOP_CHROMIUM?{executablePath:process.env.NEXLOOP_CHROMIUM}:{})});
const hscroll=page=>page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
try{
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin,viewport:{width:1440,height:900}});const page=await context.newPage();
  await page.goto('/workbench/overview');await page.locator('#wb-username').waitFor({timeout:20000});
  await page.fill('#wb-username',session.owner.username);await page.fill('#wb-password',readFileSync(session.owner.password_file,'utf8').trim());await page.click('button[type=submit]');
  await page.locator('.workbench-page h1').waitFor({timeout:20000});
  result.nav=await page.locator('#workbench-nav .nav-group').evaluateAll(gs=>gs.map(g=>({title:g.querySelector('.nav-group-title')?.textContent,pages:[...g.querySelectorAll('a')].map(a=>a.textContent)})));
  await page.locator('#workbench-nav button[aria-controls="nav-sub-ontology"]').click();
  result.nav_children=await page.locator('#nav-sub-ontology a').allTextContents();
  await page.locator('#workbench-nav a',{hasText:'企业世界模型'}).click();
  await page.locator('.workbench-page h1',{hasText:'企业世界模型'}).waitFor({timeout:20000});await page.locator('.world-summary').waitFor({timeout:20000});
  result.summary=await page.locator('.world-summary li').allInnerTexts();
  await page.screenshot({path:`${shots}/world-1440.png`,fullPage:true});result.hscroll_1440=await hscroll(page);
  // Property from the sidebar: its own page and path.
  await page.locator('#nav-sub-ontology a',{hasText:'Property'}).click();await page.locator('table[aria-label="Property"]').waitFor({timeout:20000});
  result.url_property=new URL(page.url()).pathname+new URL(page.url()).search;result.title_property=await page.locator('.workbench-page h1').innerText();
  result.withheld=await page.locator('table[aria-label="Property"] tr',{hasText:'spending_band'}).innerText();
  // Object Type: the table, then keyboard only - Tab to the Graph switch, Enter; Tab into the graph, Tab to the first node, Enter.
  await page.locator('#nav-sub-ontology a',{hasText:'Object Type'}).click();await page.locator('table[aria-label="Object Type"]').waitFor({timeout:20000});
  result.table_rows=await page.locator('table[aria-label="Object Type"] tbody tr').count();
  result.switch=await page.locator('.segmented[aria-label="查看方式"] button').allTextContents();
  await page.locator('.segmented button',{hasText:'Table'}).focus();await page.keyboard.press('Tab');await page.keyboard.press('Enter');
  await page.locator('svg[aria-label^="Object Type 关系图"]').waitFor({timeout:20000});
  const graph=page.locator('svg[aria-label^="Object Type 关系图"]');
  result.graph_label=await graph.getAttribute('aria-label');
  result.graph={layout:await graph.getAttribute('data-layout'),label_px:Number(await graph.getAttribute('data-label-px')),edges:Number(await graph.getAttribute('data-edges')),
    box:await graph.evaluate(svg=>{const r=svg.getBoundingClientRect();return {w:Math.round(r.width),h:Math.round(r.height)};}),
    node_text_px:await graph.locator('[data-node] text').first().evaluate(t=>Math.round(t.getBoundingClientRect().height))};
  await graph.focus();await page.keyboard.press('Tab');
  result.focused_node=await page.evaluate(()=>document.activeElement?.getAttribute('data-node'));
  await page.keyboard.press('Enter');await page.locator('aside.world-panel').waitFor({timeout:20000});
  result.panel=await page.locator('aside.world-panel h2').innerText();result.url_graph=new URL(page.url()).search;
  await page.screenshot({path:`${shots}/world-graph-1440.png`,fullPage:true});
  await page.goBack();await page.waitForTimeout(500);result.back_url=new URL(page.url()).search;
  result.panel_after_back=await page.locator('aside.world-panel').count();
  await page.setViewportSize({width:390,height:844});await page.goto('/workbench/ontology/properties');await page.locator('table[aria-label="Property"]').waitFor({timeout:20000});await page.waitForTimeout(500);
  await page.screenshot({path:`${shots}/world-390.png`,fullPage:true});result.hscroll_390=await hscroll(page);
  // W2 instances: choose Consumer, open the first instance, its drawer and neighbourhood; reload the deep link; Back closes it.
  await page.setViewportSize({width:1440,height:900});await page.goto('/workbench/ontology/objects');await page.locator('#world-instance-type').waitFor({timeout:20000});
  await page.selectOption('#world-instance-type','Consumer');await page.locator('table[aria-label="Consumer Object"]').waitFor({timeout:20000});
  result.instances_summary=await page.locator('.world-instances .workbench-summary').innerText();
  await page.locator('table[aria-label="Consumer Object"] tbody tr button').first().click();
  await page.locator('aside.world-object').waitFor({timeout:20000});await page.locator('aside.world-object h3',{hasText:'邻域'}).waitFor({timeout:20000});
  await page.locator('aside.world-object .world-neighbors, aside.world-object [data-empty]').first().waitFor({timeout:20000});
  result.instance_url=new URL(page.url()).pathname+new URL(page.url()).search;result.drawer=await page.locator('aside.world-object').innerText();
  result.drawer_link=await page.locator('aside.world-object a[href^="/workbench/consumers/"]').count();
  await page.screenshot({path:`${shots}/world-instance-1440.png`,fullPage:true});
  await page.reload();await page.locator('aside.world-object').waitFor({timeout:20000});result.drawer_after_reload=await page.locator('aside.world-object').count();
  await page.goBack();await page.waitForTimeout(500);result.drawer_after_back=await page.locator('aside.world-object').count();result.back_after_drawer=new URL(page.url()).pathname+new URL(page.url()).search;
  // W4 data assets: the owner's metrics read; every section present, nothing to fill in, no sideways scroll on a phone.
  await page.setViewportSize({width:1440,height:900});await page.goto('/workbench/data_assets');
  await page.locator('.workbench-page h1',{hasText:'数据资产'}).waitFor({timeout:20000});await page.locator('article[aria-label="召回索引"]').waitFor({timeout:20000});
  result.assets_sections=await page.locator('.card-grid > article').evaluateAll(a=>a.map(x=>x.getAttribute('aria-label')));
  result.assets_text=await page.locator('.workbench-page').innerText();result.assets_forms=await page.locator('.workbench-page form, .workbench-page textarea').count();
  await page.screenshot({path:`${shots}/data-assets-1440.png`,fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(400);result.assets_hscroll_390=await hscroll(page);
  // W3 capability catalog: in the 管理 group; its dispatch outcome reads exactly as the settings page (same source and wording).
  await page.setViewportSize({width:1440,height:900});await page.goto('/workbench/capabilities');
  await page.locator('.workbench-page h1',{hasText:'能力目录'}).waitFor({timeout:20000});await page.locator('[role=tablist][aria-label="能力目录"]').waitFor({timeout:20000});
  result.capabilities_tabs=await page.locator('[role=tablist][aria-label="能力目录"] [role=tab]').allInnerTexts();
  await page.locator('[role=tablist][aria-label="能力目录"] [role=tab][aria-selected=true]').focus();await page.keyboard.press('End');
  await page.locator('.rd-outcome').waitFor({timeout:20000});
  result.capabilities_outcome=await page.locator('.rd-outcome').innerText();result.capabilities_url=new URL(page.url()).search;
  result.capabilities_forms=await page.locator('.workbench-page form, .workbench-page textarea').count();
  await page.screenshot({path:`${shots}/capabilities-dispatch-1440.png`,fullPage:true});
  await page.goto('/workbench/settings');await page.locator('.rd-outcome').waitFor({timeout:20000});
  result.settings_outcome=await page.locator('.rd-outcome').innerText();
  await page.setViewportSize({width:390,height:844});await page.goto('/workbench/capabilities?tab=actions');await page.locator('[role=tabpanel]').waitFor({timeout:20000});
  await page.waitForTimeout(500);await page.screenshot({path:`${shots}/capabilities-390.png`,fullPage:true});result.capabilities_hscroll_390=await hscroll(page);
  await context.close();
}finally{await browser.close();}
writeFileSync(env('NEXLOOP_WORLD_E2E_RESULT_FILE'),JSON.stringify(result));
