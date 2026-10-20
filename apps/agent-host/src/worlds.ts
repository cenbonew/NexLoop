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

/** NX-041 S6 (D3, D6): the world group a Host process serves. real: real and test Runs; simulation: sim:* and shadow:* Runs only. */
export type WorldGroup='real'|'simulation';
export function worldGroupAccepts(group:WorldGroup,world:unknown):boolean{
  const kind=worldKind(world);
  if(kind===null)return false;
  return group==='simulation'?kind==='simulation'||kind==='shadow':kind==='real'||kind==='test';
}
/** The directory of one simulation world under a simulation Host's runtime root ('sim:<uuid>' -> 'sim-<uuid>'); null otherwise. */
export function worldDirectory(world:unknown):string|null{
  const kind=worldKind(world);
  return kind==='simulation'||kind==='shadow'?(world as string).replace(':','-'):null;
}
/** NX-041 S7 (design §6, D3): S7's names for the same rule; the S6 implementation above decides (one world group per Host). */
export function parseWorldGroup(value:unknown):WorldGroup{
  if(value===undefined)return 'real';
  if(value==='real'||value==='simulation')return value;
  throw new Error('runtime configuration refused');
}
export function groupAccepts(group:WorldGroup,world:unknown):boolean{
  return worldGroupAccepts(group,world);
}
