/**
 * 实验空间 (UI batch 3): the next-stage page, on the kit (PageHeader and the one not_enabled state, UI spec §2/§8). Nothing to read
 * or operate yet; experiments are a later stage.
 */
import {PageHeader,EmptyState} from '../kit';

export function Experiments({title}:{title:string}){
  return <section className="workbench-page" aria-labelledby="workbench-page-title">
    <PageHeader title={title} lead="属于下一阶段；当前只占位，不提供任何操作。"/>
    <EmptyState kind="not_enabled" subject="实验空间"/>
  </section>;
}
