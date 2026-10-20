/**
 * NX-041 S1 (design D1, D9): the one world check, the same rule as packages/eios-core/src/nexloop_eios/worlds.py and SQL
 * runtime.nexloop_world_valid / nexloop_world_kind. A world is real, test, sim:<lowercase uuid> or shadow:<lowercase uuid>; no other
 * reserved name ('simulation' is a data mode, not a world); anything that is not a string is not a world. The shared cases are in
 * tests/support/nx041-world-cases.json (read by the Host test and the Python test alike).
 */
export type WorldKind='real'|'test'|'simulation'|'shadow';
const EXPERIMENT=/^(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function validWorld(world:unknown):world is string{
  return typeof world==='string'&&(world==='real'||world==='test'||EXPERIMENT.test(world));
}

/** real | test | simulation | shadow, or null for anything that is not a world. */
export function worldKind(world:unknown):WorldKind|null{
  if(!validWorld(world))return null;
  if(world==='real'||world==='test')return world;
  return world.startsWith('sim:')?'simulation':'shadow';
}

/**
 * NX-041 S7 (design §6, D3): a Host process belongs to one world group. The real group (the default) runs real and test Runs only; the
 * simulation group runs sim:/shadow: Runs only. A Run of the other group is refused at dispatch, before any guard request, whatever
 * its command says (its mode already has to equal its world's kind).
 */
export type WorldGroup='real'|'simulation';
export function parseWorldGroup(value:unknown):WorldGroup{
  if(value===undefined)return 'real';
  if(value==='real'||value==='simulation')return value;
  throw new Error('runtime configuration refused');
}
export function groupAccepts(group:WorldGroup,world:unknown):boolean{
  const kind=worldKind(world);
  if(kind===null)return false;
  return group==='real'?kind==='real'||kind==='test':kind==='simulation'||kind==='shadow';
}
