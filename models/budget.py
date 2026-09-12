"""任务预算（V2.1 §二）：把「一个 max_steps 包打天下」拆成三个互相独立的预算。

旧实现里 `max_steps` 同时被当成「观察次数」「动作次数」「模型调用次数」的上限，
而 Runtime 实际统计的 `step_index` 是 Observe 次数——于是「最多 10 个动作」会变成
「最多 Observe 10 次 ≈ 5 个动作」，且观察失败也白白消耗预算。

V2.1 明确拆成：
- execution_step   ：真正执行了多少个 Agent Action（DONE 不计、Observe 不计）
- observation_count：观察了多少次（Observe 失败也消耗）
- model_call_count ：调用了多少次 VLM（规划 / 重规划 / 验证）
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TaskBudget(BaseModel):
    """任务的资源预算。命中任意一个上限即终止，而不是「再试一次」。"""

    max_action_steps: int = Field(default=20, ge=1, le=200, description="真正执行的 Agent Action 数量上限")
    max_observations: int = Field(default=60, ge=1, le=500, description="观察次数上限（Observe 失败也消耗）")
    max_model_calls: int = Field(default=40, ge=1, le=500, description="VLM 调用次数上限")

    @classmethod
    def from_max_steps(cls, max_steps: int) -> "TaskBudget":
        """兼容旧 API：`max_steps` 只约束动作步数，其余取默认值。"""
        return cls(max_action_steps=max(1, min(200, max_steps)))
