# 执行单：方案 B M2.1 上线（效应 Action 跟随所选 Consumer 版本）

内容：迁移 `task_b_m2_1_effect_action_version`（合批时编号）。改动范围和偏离见 `docs/planning-drafts/b-m2-1-notes-20261112.md`。

## 0. 时间窗口与部署约束

- **部署不得落在 P5 执行单 §4–§6 之间。**
- **L6 F1 / R1：禁止通过人工、脚本或自动入口准入任何 `:N`；只有 M2.2 与 Role 签发链补项均完成并经独立复核后，才可评估解除此禁令。**
  - 也就是不得调用 `control.nexloop_admit_effect_action_successor`，也不得运行任何会调用它的脚本。
  - 原因：准入了 `:N` 并完成 follow 之后，bind、签发和执行都会切到 `:N`，但 context pack 在 M2.2 之前仍写 `:1`（契约 v7）。Host 接受 v8 不能修复 Role launcher 仍签发配置 `:1` 的遗漏；M2.2 上线本身不构成准入授权。Role 补项属于 M2.1 签发链，可在 M2.2 集成，但须修复和独立复核后另行决定准入。
  - **G1 选 (a)：逐租户 owner 只读核对**。使用已授权且可 `SET ROLE nexloop_owner` 的维护连接；不授予应用角色表权限，不使用超级用户或绕过 RLS。`tenant_id` 来自已核实的部署租户清单，全局 effect_actions 阳性只证明表可读，不证明清单完整。清单必须具有可信来源、覆盖范围与核对时间；清单为空、遗漏任一租户或角色验证失败均 blocked，单租户结果不能称全库为零。
  - 对清单中每个租户执行以下 SQL（`psql -X -v ON_ERROR_STOP=1 -v tenant_id=<已核实租户> -f ...`；连接信息由现有安全环境提供）：

    ```sql
    begin transaction read only;
    set local role nexloop_owner;
    set local row_security = on;
    select set_config('eios.tenant_id', :'tenant_id', true);
    do $guard$
    begin
      if current_user <> 'nexloop_owner'
         or coalesce(current_setting('eios.tenant_id', true), '') = ''
         or exists(select 1 from pg_roles where rolname=current_user and (rolsuper or rolbypassrls))
         or not exists(select 1 from pg_class where oid='control.nexloop_effect_action_admissions'::regclass
                       and relowner=current_user::regrole and relrowsecurity and relforcerowsecurity) then
        raise exception 'G1 owner / tenant / FORCE RLS verification failed';
      end if;
      if (select count(*) from control.nexloop_effect_actions) = 0 then
        raise exception 'G1 positive control is empty';
      end if;
      if (select count(*) from control.nexloop_effect_action_admissions where retired_at is null) <> 0 then
        raise exception 'G1 active admissions present: stop before M2.2';
      end if;
    end $guard$;
    select current_user, current_setting('eios.tenant_id') as verified_tenant,
           (select count(*) from control.nexloop_effect_actions) as positive_control,
           (select count(*) from control.nexloop_effect_action_admissions where retired_at is null) as active_admissions;
    rollback;
    ```

    **只有正常执行、阳性对照大于 0、全部租户 active_admissions 为 0 才满足此项。报错不等于 0**（包括权限不足、租户未设置、查询失败）；任何报错或非零结果均停止并上报调度员。禁止忽略 `ON_ERROR_STOP` 或把失败转换成零。
    本地合成 PG 测试用实际非零准入验证该脚本会拒绝，并验证缺 GUC 时 owner 会静默看到零；不代表已在 stage 执行。
- M2.3（在发布事务内准入，H1 (a)）在 M2.1、M2.2 及 Role 签发补项均完成并独立复核前不得启用；必须有阻止提前部署的机制。

## 1. 部署与迁移

1. 部署包含本提交的镜像并迁移。迁移尾与 `versions.lock.json` 的 `bootstrap_revision` 相同。
2. 迁移后只读核对：

   ```sql
   select to_regprocedure('authz.nexloop_effect_bind_action_resource(text,text,text,integer)') is not null,
          to_regprocedure('authz.nexloop_effect_consumer_action_resources(text,text,text)') is not null,
          has_function_privilege('nexloop_api','authz.nexloop_effect_consumer_action_resources(text,text,text)','execute');
   ```

   三列都应为 true。

## 2. 冒烟（行为应与上线前相同）

1. 没有任何准入时，所有顾客的选择结果都是 `:1`。v2 顾客由 A′ 证明，`control.nexloop_effect_scope_proofs` 照常增加。
2. 看一次 relay 回复，检查新 Run 的 `authz.nexloop_run_credentials.allowed_resources` 是 `{eios:action:nexloop.service.request:1}`，新 context 的 `action_resource` 是 `…:1`。
3. `relay_reply_failing` 不应升高。

## 3. 回退

- 回到上一个镜像。M2.1 期间没有任何准入，所以所有 context 的 `action_resource` 都是 `:1`，旧代码不读新列，行为与以前相同。
