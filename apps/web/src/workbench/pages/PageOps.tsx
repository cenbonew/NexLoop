/**
 * Web pages batch 6 (owner walkthrough 2026-10-18, F02/F07/F11). Operations: the governed controls after the facts (object, state,
 * evidence, pending work), not above them as Page's action slot puts them. A page passes its `actions` here instead of to Page.
 * (UX audit C1: the former IdRef and NoWrap are the kit's IdText and the `nowrap` class.)
 */
import type {ReactNode} from 'react';

export function Operations({actions,title='操作'}:{actions?:ReactNode;title?:string}){
  if(!actions)return null;
  return <section className="workbench-ops" aria-labelledby="workbench-ops-title">
    <h2 id="workbench-ops-title">{title}</h2>
    <div className="workbench-actions" role="group" aria-label="可执行的操作">{actions}</div>
  </section>;
}
