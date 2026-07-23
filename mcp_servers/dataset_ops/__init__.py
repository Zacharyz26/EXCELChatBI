"""数据集变换与聚合预览确定性工具组（v2.3 阶段 2，设计文档 14.7）。

当前生产 Agent 只提供结构化参数、枚举白名单的 transform_dataset（衍生数据集，
带血缘）与 aggregate_preview（分组聚合出表）。受限 SQL 已进入独立安全项目，
通过评审前不改变本模块的运行边界。当前以进程内 ``Tool.invoke`` 运行，v2.4
迁移到 data-tools MCP Server。
"""
