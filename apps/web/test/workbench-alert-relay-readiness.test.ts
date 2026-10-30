// Alert rules v23 (S43, relay readiness): the five new rules are named and explained in business words like the rest, never the bare
// rule id, and point to the operations page where the relay is checked.
import {expect,test} from 'vitest';
import {ALERT_INFO} from '../src/workbench/pages/alertInfo';

const IDS=['relay_dependency_expiring','relay_dependency_expired','relay_unserviceable_enrollment','relay_no_active_enrollment','relay_stalled_consumers'];
test('relay readiness rules: a Chinese name and help, a count, the operations page',()=>{
  for(const id of IDS){const info=ALERT_INFO[id];expect(info,id).toBeDefined();
    expect(info.name).toMatch(/\p{Script=Han}/u);expect(info.name).not.toContain(id);expect(info.help).toMatch(/\p{Script=Han}.*。$/u);
    expect(info.unit).toBe('count');expect(info.page).toBe('operations');}
  expect(new Set(IDS.map(id=>ALERT_INFO[id].name)).size).toBe(IDS.length);  // five distinct names
});
