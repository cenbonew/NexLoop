// Development machine only (scripts/dev/workbench_smoke.sh): the Web dev server on loopback HTTP, /api proxied to the
// loopback HTTPS API. The API's browser realms are fixed HTTPS origins and check Origin/Host, so the proxy presents the
// API's own origin; the API's self-signed one-day certificate is accepted only by this proxy (never by the browser).
import {fileURLToPath} from 'node:url';
const api=process.env.NEXLOOP_DEV_SMOKE_API_ORIGIN;
const port=Number(process.env.NEXLOOP_DEV_SMOKE_WEB_PORT);
if(!/^https:\/\/127\.0\.0\.1:\d+$/.test(api??'')||!Number.isInteger(port))throw new Error('workbench smoke: loopback API origin and Web port required');
export default {
  root:fileURLToPath(new URL('../../apps/web',import.meta.url)),
  logLevel:'warn',
  server:{host:'127.0.0.1',port,strictPort:true,
    proxy:{'/api':{target:api,secure:false,changeOrigin:true,headers:{origin:api}}}},
};
