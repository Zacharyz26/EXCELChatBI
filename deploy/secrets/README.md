# 本地 Compose secrets

> 状态：现行根 Compose 安全约定 · 复核日期：2026-10-10

本目录不跟踪任何运行时 secret。根 Compose 必须显式通过
`AUTH_TOKENS_FILE_PATH` 与 `MCP_*_FILE_PATH` 指向部署系统创建的独立文件；缺失时配置
直接失败，不使用公开默认值。五个服务 token 不能复用，上下文 HMAC key 也不得与服务
token 相同。

CI/Compose E2E 通过 `scripts/compose_test_env.sh` 在系统临时目录中生成公开合成凭据，并在
退出时清理。它们不得复制到 staging/production；本地验收无需读取仓库 `.data`。

`AUTH_TOKENS_FILE_PATH` 指向 UTF-8 JSON 对象，键是至少 32 字符的随机 Bearer token，
值是认证主体：

```json
{
  "<at-least-32-random-characters>": {
    "user_id": "<application-user-id>",
    "tenant_id": "<application-tenant-id>",
    "roles": ["kb_admin"]
  }
}
```

尖括号内容只是 schema 占位符，不能直接用于部署。`roles` 可为空数组；`user_id` 和
`tenant_id` 必须是非空字符串。其余六个文件均为单值文本文件：五个 MCP 服务 token
