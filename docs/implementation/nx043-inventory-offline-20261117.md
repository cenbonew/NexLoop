# NX-043 inventory 独立离线切片（2026-11-17）

基线 `7109acd5cfed970c5e219cb7254e774552671291`；分支 `codex/nx043-inventory-offline-20261117`。
只改清单工具、对应离线测试和本证据文件；保留工作树，不改 planning，不宣告 NX-043 或关联 AT 完成。

## 已有约定与交付核对

`fb5b0130` 是本基线可见的清单工具及其测试交付，未发现这些路径的后续修复。
`nx043_stage.py` 通过 `s6_common.Result.write` 生成结果；`Result.document` 的顶层 passed 为布尔值或 null，write 保存单行 JSON。
既有清单只列摘要，退出 0 表示清单成功，不代表验收通过；不得输出 steps、observed、状态文件的完整 ids。
原有末行 JSON 读取行为也保留。多行 JSON 是相同结构的格式化合成副本，并非声称现场生成器写了多行格式。

## 复现、最小修复与边界

新增五个回归后，首轮实际 `3 failed, 4 passed in 0.20s`：

- `test_non_boolean_passed_cannot_impersonate_a_real_result`：字符串 `"True"` 被打印为 True，与布尔通过不可区分。首轮循环停在这一项，其余非法类型于修后执行。修为严格只展示布尔 True/False、显式 null 为 None；字段缺失或类型不合法时展示 `-`。
- `test_formatted_json_and_existing_last_line_format_are_listed`：格式化多行 JSON 被误列为 other。修为先解析完整文档，失败后才回退至既有末行 JSON 格式。
- `test_oversized_state_is_not_opened_or_parsed`：超上限 state 仍解析并输出 nonce。修为对 state 和结果统一先检查大小，实际读取最多 MAX_BYTES+1 字节，再检查实际字节数。state 超限保留 state/文件名/大小，nonce 为 `-`；普通结果超限维持 other。

修后增加缺失 passed、非对象/破损/非法 UTF-8 文档和 stat 后文件增长回归。
最终共九项：原有两项，加七项；通过/失败/阻塞结果使用真实 `Result.write` 生成，只用本地合成 step。
上限回归缩小 MAX_BYTES 以合成小文件验证同一分支，不创建大体积数据。

没有修改 `nx043_stage.py`、`s6_common.py`、现场脚本行为、验收语义、策略、权限或 SDK。
清单仍是摘要，不是 Schema 验证器或准入裁决；此变更不使畸形结果获得验收效力。

## 实际验证

以下 `$BASE_CHECKOUT` 仅在记录中代替原主检出的绝对路径，避免将本机用户路径写入公开文件；执行时使用该绝对路径。

```sh
PYTHONPATH=packages/eios-core/src "$BASE_CHECKOUT/.venv/bin/python" -m pytest -q tests/test_nx043_inventory.py -n 0
git diff --check
```

pytest 实际执行三次：首轮 `3 failed, 4 passed in 0.20s`；修后 `9 passed in 0.15s`；增加禁止连接守卫后最终 `9 passed in 0.16s`。
`git diff --check` 退出 0。最终回归的 autouse fixture 禁止 socket connect/connect_ex 与 psycopg.connect。

普通 `main` 入口由测试调用，结果生成器只写临时合成文件；没有访问服务、部署、准入、真实 READ、外发或数据库。
没有做 NX-064，没有改 planning，没有推送。真实 stage/业务验收与本切片无关，未执行。
固定提交待独立评审；出站扫描结果另随交付报告，不构成推送授权。
