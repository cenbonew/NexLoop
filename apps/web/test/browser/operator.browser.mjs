// NX-037 automated operator for the synthetic stack: plays the person who, in the real trial, holds the customer side of
// the WebChat. Headless Chromium (Playwright, pinned in apps/web/package.json) logs in through the login form and opens the
// first conversation; then, for each JSON line on stdin ({seq, act, text}), it either types the message and presses send
// (act true) or does nothing (act false, an operator who does not complete the interaction), and answers one JSON line
// ({seq, sent}) on stdout. Only the synthetic account of tests/test_nx037_real_driver_pg.py; never a real channel.
import {readFileSync} from 'node:fs';
import {createInterface} from 'node:readline';
import {chromium} from 'playwright';

const env=name=>{const value=process.env[name];if(!value)throw new Error(`${name} required`);return value;};
const session=JSON.parse(readFileSync(env('NEXLOOP_OPERATOR_SESSION_FILE'),'utf8'));
const password=readFileSync(session.password_file,'utf8').trim();
const browser=await chromium.launch({headless:true});
try{
  const context=await browser.newContext({ignoreHTTPSErrors:true,baseURL:session.origin});  // the run's own test CA, 127.0.0.1 only
  const page=await context.newPage();
  await page.goto('/');
  await page.fill('#username',session.username);await page.fill('#password',password);await page.click('button[type=submit]');
  await page.locator('select#conversation').waitFor({timeout:20000});
  await page.waitForFunction(id=>document.querySelector('select#conversation')?.value===id,session.conversation_id,{timeout:20000});
  process.stdout.write(JSON.stringify({ready:true})+'\n');
  for await(const line of createInterface({input:process.stdin})){
    const prompt=JSON.parse(line);
    if(prompt.act){
      await page.fill('#chat-message',prompt.text);
      await page.getByRole('button',{name:'发送',exact:true}).click();
      await page.locator('ol.messages > li > p',{hasText:prompt.text}).first().waitFor({timeout:20000});
    }
    process.stdout.write(JSON.stringify({seq:prompt.seq,sent:!!prompt.act})+'\n');
  }
  await context.close();
}finally{await browser.close();}
