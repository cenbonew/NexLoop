/** NX-041 S1 (D1, D9): the Host's world check uses the same cases as the Python and SQL checks (tests/support/nx041-world-cases.json). */
import {readFileSync} from 'node:fs';
import {describe,it,expect} from 'vitest';
import {validWorld,worldKind} from '../dist/worlds.js';
import {RuntimeError,validateRunCommand} from '../dist/runtime-adapter.js';
import {command} from './pi-runtime-support.ts';

const cases=JSON.parse(readFileSync(new URL('../../../tests/support/nx041-world-cases.json',import.meta.url),'utf8')) as {
  valid:{world:string;kind:string}[];invalid:unknown[];mode_pairs:{world:unknown;mode:string;ok:boolean}[]};

describe('NX-041 world check (shared cases)',()=>{
  it('accepts real, test, sim:<uuid> and shadow:<uuid> with their kind',()=>{
    for(const c of cases.valid){expect(validWorld(c.world)).toBe(true);expect(worldKind(c.world)).toBe(c.kind);}
  });
  it.each(cases.invalid.map(v=>[JSON.stringify(v),v]))('refuses %s',(_,value)=>{
    expect(validWorld(value)).toBe(false);expect(worldKind(value)).toBeNull();
  });
  it('a Run command passes only when the world kind is its mode; otherwise invalid_run_command before anything else',()=>{
    for(const p of cases.mode_pairs){
      const cmd={...command(),world_id:p.world,mode:p.mode} as unknown;
      if(p.ok)expect(validateRunCommand(cmd).world_id).toBe(p.world);
      else{
        let code:string|undefined;try{validateRunCommand(cmd);}catch(error){code=error instanceof RuntimeError?error.code:'other';}
        expect(code,JSON.stringify(p)).toBe('invalid_run_command');
      }
    }
  });
});
