"""ChatBI 单一 function-calling Agent 编排层。

确定性任务提纲只承担澄清、进度展示和审计；同一个 Agent 负责选择工具、接收工具
反馈并继续推理，最终由确定性 Evidence/Artifact Verifier 收口。
"""
