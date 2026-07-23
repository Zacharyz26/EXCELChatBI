# internal_data（v3.0 数据连接器预留目录）

内部数据接入已从“明确不做”调整为 v3.0 阶段 7 范围，但当前仍未实现、未注册到 Agent 工具集。

计划覆盖 PostgreSQL/MySQL/数仓、对象存储、内部 REST API 和 BI 语义层。连接器以域隔离
MCP Server 和独立镜像交付，持有自己的上游凭据并执行主体、项目/租户、表列行权限、只读或
写入风险、分页/限流、结果边界和审计；Host 和模型不能读取连接器凭据。

项目内工具在 v2.4 统一迁移到 MCP Client Gateway 规范路径，本地支持 stdio，容器/远程支持
Streamable HTTP；进程内适配器仅作迁移兼容或测试。阶段 7 的第三方/跨网络服务先进入
Server Catalog 候选区，经管理员准入、来源/版本/schema、数据分类、企业授权、权限和风险检查
后才能使用；远程 token 必须绑定目标 Server，禁止向上游系统透传。

现行依据：[`docs/Agent自主化开发规划.md`](../../docs/Agent自主化开发规划.md) 的 v3.0
阶段 7/8；项目内协议化与外部服务治理的边界见
[`docs/v2.4/MCP与Docker架构决策.md`](../../docs/v2.4/MCP与Docker架构决策.md)，连接器
Server Catalog、授权、镜像与多实例设计见
[`docs/MCP与Docker全阶段演进设计.md`](../../docs/MCP与Docker全阶段演进设计.md)。
