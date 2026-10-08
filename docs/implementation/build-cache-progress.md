# Community 构建依赖层缓存

2026-10-08：调整 `deploy/community/Dockerfile`。先 COPY hash 锁定的 requirements 并独立执行 `pip install --require-hashes`，随后才 COPY 业务 wheel，以 `--no-deps` 安装。业务 wheel 改变时不再使前面的固定依赖安装层失效；首次构建或依赖/base image 改变仍需安装依赖。

保持原 `PYTHON_IMAGE` 输入、hash 校验、UID/GID 10001、无登录 shell、受限构建上下文与无 secret 构建约束。没有修改镜像 digest、锁文件、scripts、planning 或 Host。

实际验证：

- `uv run pytest -q tests/test_community_container_contract.py -k 'root_docker_context or worker_is_separately'`：2 passed，3 deselected，0.32s；只选择不会构建 Node 的现有边界用例，没有首次失败。
- `uv run python -` 的只读断言检查：requirements 安装先于 wheel COPY，wheel 安装保留 no-deps，两个 RUN 层，原 base 参数和 USER/UID 保留，文件没有 MODEL_/env 引入；通过。

没有执行 Docker build、Compose、Node build 或完整 CI。实际缓存命中与耗时收益尚未测量；这里证明的是层依赖顺序，不声称镜像运行验收完成。

后续主 Agent 实测：新11服务0035 Compose与完整CI986 Python/24 SQLite
通过。第一构建依赖安装层161.3秒；随后仅在新的私有纯公开文件构建目录
把wheel换为此前实测0034版本，执行
`uv run python .ci-results/check_dependency_cache.py`，依赖层CACHED、wheel
COPY未CACHED，完整第二构建1.36秒。证据：build-cache-evidence.json。
这是一次实际缓存命中，不是统计性能基准；cache probe未启动容器或推送镜像。
