# 本地模型环境入口

默认 launcher 继续只传 PATH/LANG/LC_ALL/TMPDIR。新增显式 `--allow-local-model-key` 仅允许 `MODEL_API_KEY`；要求实际私有 Host 配置选择 `deepseek-flash`，其私有 model 配置明确 `stage=false`。不读取 `.env`、不透传 `DEEPSEEK_API_KEY`、DSN、渠道或其它环境变量。

本地操作者在已有安全环境中设置 MODEL_API_KEY 后，可给标准启动命令追加该选项；凭据值不放 argv。私有 MODEL_CREDENTIALS_FILE 非空时依旧优先。stage 必须省略该选项，使用私有 secret 文件；误加选项直接拒绝，不能因 file 已有 key 而允许 env 进入 Node。

缺 key 时保留 deterministic fallback，不声称真实模型验证通过。测试只用 dummy key，实际 Node exec 与冻结 selector/getAuth，不触发模型请求。stage 部署仍需单独验证。
