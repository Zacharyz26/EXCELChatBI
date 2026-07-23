"""治理与安全层：当前进程内工具和 v2.4 标准 MCP 调用都必须经过。

落点对应红线：
- schema_validator  → 红线3 工具入参必过 schema 校验
- permissions       → 红线7 权限前置 / 白名单
- sandbox           → 红线5 代码执行必入沙箱
- audit             → 红线7 敏感操作留审计
- observability     → 全链路 trace（模型/工具/耗时/token/成本）
- policy            → v2.4 中央策略网关（静态准入、项目范围、预算）
"""
