# ADR-026｜项目许可证保持 MIT

状态：**采用**（负责人 2026-10-11 确认，原话“保持 MIT”）。

关联：docs/handoff/docs/16_ADR.md 中 ADR-016（开源许可证建议）；`LICENSE`、`NOTICE`、`licenses/`；docs/implementation/S0-report.md（NX-005 许可与来源方案）。

## 1. 背景

交接规格 ADR-016 建议 NexLoop 采用 Apache-2.0。S0 阶段（NX-005）实际建立的是 MIT：仓库根目录的 `LICENSE` 为 MIT，`NOTICE` 与 `licenses/` 保留了第三方来源与原许可证文本（含 vendored NEX-EIOS 内核、Pi 冻结源码的 MIT 文本）。仓库已经以 MIT 公开发布。编写开发者架构说明时发现，`LICENSE` 与交接规格的建议不一致，需要明确取舍。

## 2. 决定

1. NexLoop 自身代码继续采用 **MIT**，不改为 Apache-2.0。
2. 第三方代码保持各自原许可证，来源、提交与许可证文本继续记录在 `NOTICE`、`licenses/` 与 `versions.lock.json`。
3. 交接规格 ADR-016 中“建议 Apache-2.0”的表述由本 ADR 取代。冻结的交接包不修改，以本 ADR 为准。

## 3. 后果

- 与已公开的 `LICENSE` 一致，不需要重新授权或改动已有文件头。
- MIT 不包含 Apache-2.0 的明确专利授权与专利报复条款；如果将来需要这类条款，须另立 ADR 并由负责人决定，并评估对已有贡献的影响。
- 新增第三方依赖时，仍按 S0 的流程核对许可证兼容性并更新 `NOTICE` 与 `licenses/`。
