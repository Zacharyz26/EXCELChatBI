"""治理与安全层：所有 MCP 调用必经。

落点对应红线：
- schema_validator  → 红线3 工具入参必过 schema 校验
- permissions       → 红线7 权限前置 / 白名单
- sandbox           → 红线5 代码执行必入沙箱
- audit             → 红线7 敏感操作留审计
- observability     → 全链路 trace（模型/工具/耗时/token/成本）
"""
