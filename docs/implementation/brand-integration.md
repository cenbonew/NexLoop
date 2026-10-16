# NexLoop 品牌接入（NX-028 补充）

2026-10-16，项目负责人选定折带 N 标志。共用组件 `apps/web/src/Brand.tsx` 在普通入口（含登录及会话错误状态）和负责人工作台页头显示组合标志。中英文 README 使用同一素材。浏览器与触屏收藏图标使用独立 N，保存在 `apps/web/public/brand/`，构建会复制到发布目录，不依赖本机视频工程路径。

素材：已批准的透明组合标志原样复制；独立图标由内置 imagegen 参考该素材生成。提示词要求仅保留同造型折带 N，蓝色外侧、深蓝内侧、透明正方形背景，不附文字。使用方式见素材目录 README。图标是 PNG，不声称是可编辑矢量。

实际检查：`pnpm --filter @nexloop/web build`（Node 24），TypeScript 与 Vite 通过；`git diff --check` 通过；`.venv/bin/python docs/handoff/tools/validate_handoff.py --planning planning` 通过。首次用系统 Python 运行校验器因缺少 yaml 失败，改用项目虚拟环境后通过。构建命令首次执行后又补齐图标并重跑通过。

Ego 浏览器实际加载工作台，组合标志 naturalWidth 正常，显示为 240×80，无横向溢出，favicon 指向独立图标。截图调用发生 CDP 超时，已尝试同一浏览器空间恢复；因此不以截图作为已完成证据。未启动业务后端，页面处于连接错误状态，本轮只验证品牌显示，不声称完成登录或真实业务验证。390×844 移动视口中标志已加载，显示为 190×63，无横向溢出；恢复后截图再次超时。

任务状态不变；本次为 NX-028 的品牌补充。没有部署或远程同步。
