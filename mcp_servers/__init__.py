"""确定性工具服务集合。

生产当前通过进程内 ``Tool.invoke`` 运行；标准 MCP Tool Contract、官方 SDK
stdio Server adapter 和 Client Gateway 已落地。完成双传输探针、服务认证和
阶段 2 执行切换后，才能按服务组独立部署。新增分析能力优先做成零 LLM 的
工具能力，而非塞进编排层。
"""
