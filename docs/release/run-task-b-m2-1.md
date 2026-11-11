# 执行单：方案 B M2.1 上线（效应 Action 跟随所选 Consumer 版本）

内容：迁移 `task_b_m2_1_effect_action_version`（合批时编号）。改动范围和偏离见 `docs/planning-drafts/b-m2-1-notes-20261112.md`。

## 0. 时间窗口与部署约束

- **部署不得落在 P5 执行单 §4–§6 之间。**
- **L6 F1：从 M2.1 上线到 M2.2 上线这段时间里，stage 上不得通过 configurator 手动准入任何 `:N`。**
  - 也就是不得调用 `control.nexloop_admit_effect_action_successor`，也不得运行任何会调用它的脚本。
  - 原因：准入了 `:N` 并完成 follow 之后，bind、签发和执行都会切到 `:N`，但 context pack 在 M2.2 之前仍写 `:1`（契约 v7）。M2.2 上线、Host 能接受 v8 之后，才能开始准入。
  - 只读核对（迁移后执行一次，M2.2 上线前每次部署都要再执行）：

    ```sql
    select count(*) from control.nexloop_effect_action_admissions where retired_at is null;
    ```

    结果应为 0。不为 0 就停止，并上报调度员。
- M2.3（在发布事务内准入，H1 (a)）在 M2.1 和 M2.2 都上线之前不得部署。

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
