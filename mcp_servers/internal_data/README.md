# internal_data（占位，MVP 暂不实现）

内部数据接入工具（需求 F5），属**阶段三**范围，当前仅占位目录。

实现时需提供：
- `query_db` / `call_api` 两类工具
- 按用户 / 租户**权限前置过滤**（红线7）
- 调用**留审计**（`packages/governance/audit.py`）

> 待确认（CLAUDE 第9节）：内部数据源清单与权限模型。需先停下与负责人确认后再实现。
