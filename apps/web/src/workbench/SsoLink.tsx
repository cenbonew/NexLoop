/**
 * NX-058 slice F: single sign-on in the workbench UI. The login card's "用 SSO 登录" buttons appear only for configured providers (none
 * while SSO is off); each is a plain link to the start endpoint, a top-level navigation, because the IdP redirects the browser back.
 * The members page offers an owner the link and unlink of an operator's or reviewer's IdP subject (the server rechecks the role, the
 * status and the provider; owner targets are refused until their approval policy exists).
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readSsoProviders,ssoStartPath,type Me,type MemberRow,type SsoProvider} from './api';
import {Form} from './actions';
import {ChoiceGroup} from './kit';

const useProviders=()=>useQuery({queryKey:['workbench','sso-providers'],queryFn:readSsoProviders,refetchOnWindowFocus:false,staleTime:Infinity});

/** The login card's SSO entry; nothing at all without a configured provider. `failed`: the callback came back refused (one answer). */
export function SsoButtons({providers,failed}:{providers:SsoProvider[];failed:boolean}){
  if(!providers.length)return null;
  return <div className="sso-login">
    {failed?<p className="error" role="alert">单点登录没有完成。请重试；如果仍然不行，请联系负责人确认你的账号已绑定。</p>:null}
    {providers.map(p=><a key={p.provider_id} className="button btn-secondary" href={ssoStartPath(p.provider_id)}>用 SSO 登录（{p.display_name}）</a>)}
  </div>;
}

export function WorkbenchSso(){
  const providers=useProviders();
  const failed=typeof window!=='undefined'&&new URLSearchParams(window.location.search).get('sso')==='failed';
  return <SsoButtons providers={providers.data??[]} failed={failed}/>;
}

/** Which link controls a row allows: active operators and reviewers other than the owner itself. */
export function ssoControls(row:MemberRow,self:string):boolean{
  return row.principal_id!==self&&!row.erased&&row.member_status==='active'&&(row.role==='operator'||row.role==='reviewer');
}

export function SsoLinkForms({member,row,providers}:{member:Me;row:MemberRow;providers:SsoProvider[]}){
  const [provider,setProvider]=useState(providers.length===1?providers[0].provider_id:'');
  const [subject,setSubject]=useState('');const [mode,setMode]=useState<'sso_or_password'|'sso_only'|''>('');
  const id=(s:string)=>`sso-${s}-${row.label}`;
  const valid=!!provider&&!!subject.trim()&&subject.trim().length<=255&&!!mode;
  return <>
    {providers.length?<Form operation="link_workbench_external" member={member} title="绑定单点登录" disabled={!valid}
      body={()=>valid?{target_principal:row.principal_id,provider_id:provider,external_subject:subject.trim(),login_mode:mode}:null}>
      <p>对象：{row.username??'（无账户）'} {row.label}</p>
      {providers.length>1?<fieldset className="choices"><legend>身份提供方</legend>{providers.map(p=><label key={p.provider_id} className="choice">
        <input type="radio" name={id('provider')} checked={provider===p.provider_id} onChange={()=>setProvider(p.provider_id)}/>{p.display_name}</label>)}</fieldset>
        :<p>身份提供方：{providers[0].display_name}</p>}
      <div className="field"><label htmlFor={id('subject')}>IdP 用户标识（sub）</label><input id={id('subject')} value={subject} onChange={e=>setSubject(e.target.value)}
        required maxLength={255} autoComplete="off" spellCheck={false}/></div>
      <fieldset className="choices"><legend>登录方式</legend>
        <label className="choice"><input type="radio" name={id('mode')} checked={mode==='sso_or_password'} onChange={()=>setMode('sso_or_password')}/>SSO 或密码</label>
        <label className="choice"><input type="radio" name={id('mode')} checked={mode==='sso_only'} onChange={()=>setMode('sso_only')}/>仅 SSO</label></fieldset>
      <p className="impact-summary" role="note">影响：IdP 中这个用户只能登录到这位成员。选“仅 SSO”时，密码不再能登录，也不能再签发重置码，现有会话立即失效。</p>
    </Form>:<p className="note">未配置单点登录的身份提供方，不能绑定。</p>}
    <Form operation="unlink_workbench_external" member={member} title="解绑单点登录" body={()=>({target_principal:row.principal_id})}>
      <p>对象：{row.username??'（无账户）'} {row.label}</p>
      <p className="impact-summary" role="note">影响：删除这位成员的单点登录绑定，会话立即失效；之后只能用密码登录。没有绑定时服务端拒绝。</p>
    </Form>
  </>;
}

/** The members page's hook: the link forms of one row, with the configured providers. */
export function SsoLinkControls({member,row}:{member:Me;row:MemberRow}){
  const providers=useProviders();
  if(!ssoControls(row,member.principal_id))return null;
  return <SsoLinkForms member={member} row={row} providers={providers.data??[]}/>;
}

/** The invite form's login method: password, or SSO only with a configured provider (none offered while SSO is off). '' is password. */
export function SsoInviteChoiceView({providers,value,onChange}:{providers:SsoProvider[];value:string;onChange:(value:string)=>void}){
  if(!providers.length)return null;
  return <ChoiceGroup legend="登录方式" name="invite-login" value={value} onChange={onChange} options={[
    {value:'',label:'密码',hint:'接受邀请时由本人设置密码。'},
    ...providers.map(p=>({value:p.provider_id,label:`仅 SSO（${p.display_name}）`,hint:'接受邀请时不设密码；负责人绑定本人的 SSO 账号后才能登录。'}))]}/>;
}
export function SsoInviteChoice(props:{value:string;onChange:(value:string)=>void}){
  const providers=useProviders();
  return <SsoInviteChoiceView providers={providers.data??[]} {...props}/>;
}
