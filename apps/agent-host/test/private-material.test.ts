import {describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,symlinkSync,linkSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {readPrivateMaterial} from '../src/private-material.js';
import {selectTrustedModel} from '../src/trusted-model-profile.js';
describe('actual private filesystem model material',()=>{
 it('empty file permits model fallback but refuses guard/config material',()=>{
  const root=mkdtempSync(join(tmpdir(),'nexloop-model-material-'));chmodSync(root,0o700);
  try{const key=join(root,'key');writeFileSync(key,'',{mode:0o600});
   expect(()=>readPrivateMaterial(key,64)).toThrow();
   expect(selectTrustedModel({MODEL_CREDENTIALS_FILE:key},readPrivateMaterial).runtimeProfile).toBe('deterministic-test');
   expect(selectTrustedModel({MODEL_CREDENTIALS_FILE:key,MODEL_API_KEY:'synthetic-environment'},readPrivateMaterial).credentialSource).toBe('environment');
   expect(selectTrustedModel({MODEL_CREDENTIALS_FILE:key},readPrivateMaterial,true).runtimeProfile).toBe('deterministic-test');
  }finally{rmSync(root,{recursive:true});}
 });
 it('refuses actual public-mode, symlink, hardlink and oversize files',()=>{
  const root=mkdtempSync(join(tmpdir(),'nexloop-model-material-'));chmodSync(root,0o700);
  try{const key=join(root,'key');writeFileSync(key,'synthetic-key',{mode:0o600});
   expect(()=>readPrivateMaterial(key,2,true)).toThrow();chmodSync(key,0o644);expect(()=>readPrivateMaterial(key,64,true)).toThrow();chmodSync(key,0o600);
   symlinkSync(key,join(root,'alias'));expect(()=>readPrivateMaterial(join(root,'alias'),64,true)).toThrow();
   linkSync(key,join(root,'linked'));expect(()=>readPrivateMaterial(key,64,true)).toThrow();
  }finally{rmSync(root,{recursive:true});}
 });
 it.each([{MODEL_ID:'other'},{MODEL_BASE_URL:'https://untrusted.invalid'},{MODEL_PROVIDER:'test',MODEL_ID:'other'}])('rejects explicit bad model configuration without a credential',env=>{
  expect(()=>selectTrustedModel(env,readPrivateMaterial)).toThrow('runtime_model_configuration_refused');
 });
});
