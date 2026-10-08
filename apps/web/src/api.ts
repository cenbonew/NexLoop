export type Session = {authenticated:true; tenant_id:string; principal_id:string; restricted:boolean};
export class ApiError extends Error { constructor(readonly status:number) { super('请求未完成'); } }
async function request(path:string, init?:RequestInit):Promise<unknown> {
  const response=await fetch(path,{...init,credentials:'same-origin',cache:'no-store'});
  if(!response.ok) throw new ApiError(response.status);
  return response.json();
}
function session(value:unknown):Session {
  const v=value as Partial<Session>;
  if(!v || v.authenticated!==true || typeof v.tenant_id!=='string' || typeof v.principal_id!=='string' || typeof v.restricted!=='boolean') throw new Error('会话响应无效');
  return v as Session;
}
function authenticated(value:unknown):{session:Session;csrf:string} {
  const v=value as {csrf_token?:unknown};
  if(typeof v?.csrf_token!=='string' || !v.csrf_token) throw new Error('会话响应无效');
  return {session:session(value),csrf:v.csrf_token};
}
export async function readSession():Promise<Session|null> {
  try {return session(await request('/api/v1/auth/session'));}
  catch(e) {if(e instanceof ApiError && e.status===401) return null; throw e;}
}
export async function login(username:string,password:string) {
  return authenticated(await request('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})}));
}
export async function refreshCsrf() {return authenticated(await request('/api/v1/auth/csrf',{method:'POST'}));}
export async function logout(csrf:string) {await request('/api/v1/auth/logout',{method:'POST',headers:{'X-CSRF-Token':csrf}});}
export function errorMessage(error:unknown):string {
  if(error instanceof ApiError && error.status===401) return '账号或密码不正确，或会话已失效。';
  if(error instanceof ApiError && error.status===429) return '尝试次数过多，请稍后再试。';
  return '暂时无法连接，请稍后重试。';
}

export async function readReadiness():Promise<boolean> {
  const response=await fetch('/health/ready',{credentials:'same-origin',cache:'no-store'});
  if(response.status!==200 && response.status!==503) throw new ApiError(response.status);
  const value=await response.json();
  if(typeof value.ready!=='boolean') throw new Error('状态响应无效');
  return value.ready;
}
