"""执行层（v4.1 §九 的那个包）：状态机 + 唯一写入口 + 启动恢复。

    state.py     —— 合法迁移表与「崩溃后该落到哪个终态」的规则
    service.py   —— ExecutionService：状态迁移与它的事件在同一个事务里
    recovery.py  —— 启动扫描：进程被杀后留下的非终态执行怎么收

放在 `agent/` 下而不是 `models/`：`models.execution` 定义的是**词汇**（一条执行是什么、
有哪些状态），这里定义的是**行为**（谁在什么时候推进它、推进失败怎么办）。
`models` 不该依赖 `storage`，而这一层要。
"""
from .recovery import recover_executions
from .service import ExecutionService

__all__ = ["ExecutionService", "recover_executions"]
