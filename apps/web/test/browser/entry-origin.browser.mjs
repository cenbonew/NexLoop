// S81 R67 (tests/test_entry_origin_real_browser.py). `build OUT`: bundles entry-origin.entry.ts (the real enterWithTicket) into
// OUT/entry.js. `drive URL`: opens URL in headless Chromium, clicks the button (a user gesture, as in the workbench) and waits for the
// window the ticket form is posted into.
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';
const [mode,arg]=process.argv.slice(2);
if(mode==='build'){
  const {build}=await import('vite');
  await build({configFile:false,logLevel:'error',define:{'process.env.NODE_ENV':'"production"'},root:fileURLToPath(new URL('../..',import.meta.url)),
    build:{outDir:arg,emptyOutDir:true,minify:false,lib:{entry:fileURLToPath(new URL('./entry-origin.entry.ts',import.meta.url)),formats:['iife'],name:'entry',fileName:()=>'entry.js'}}});
}else if(mode==='drive'){
  const browser=await chromium.launch({headless:true});
  try{
    const context=await browser.newContext();const page=await context.newPage();
    await page.goto(arg);
    const opened=context.waitForEvent('page');
    await page.click('#enter');
    const win=await opened;await win.waitForURL(/\/done$/,{timeout:10000});
  }finally{await browser.close();}
}else{throw new Error('mode');}
