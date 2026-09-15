"""Observation 与 prompt 摘要。

V2 起 history 归 `storage.trajectory_store.TrajectoryStore` 管理，不再有 AgentState：
任务状态在 `models.task.Task`，恢复点状态在 `models.checkpoint.Checkpoint`。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .action import Action


class StepOutcome(str, Enum):
    """单次观察 / 执行的结果判定。

    与 `models.task_step.StepStatus` 区分：那个描述「计划步骤走到哪了」。
    """

    OK = "ok"
    ERROR = "error"
    DONE = "done"


# 进 prompt 的字段白名单：截图路径、时间戳等噪声既费 token，也会让模型把本地文件路径当成页面信息
PROMPT_FIELDS = {"step", "action", "result", "status", "message", "package", "activity"}
# result 只保留决策真正用得上的键，避免 verbose 结果撑爆上下文
PROMPT_RESULT_FIELDS = {"ok", "error", "x", "y", "duration", "text"}


class Observation(BaseModel):
    step: int
    screenshot_path: str
    screen_size: tuple[int, int] | None = None
    package: str = ""
    activity: str = ""
    ui_tree: str | None = None
    action: Action | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    status: StepOutcome = StepOutcome.OK
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)

    def to_prompt_dict(self) -> dict[str, Any]:
        """产出进 prompt 的精简表示。

        mode="json" 让枚举序列化成 "ok" 而不是 StepOutcome.OK；result 走白名单裁剪。
        """
        data = self.model_dump(mode="json", include=PROMPT_FIELDS)
        result = self.result or {}
        data["result"] = {k: v for k, v in result.items() if k in PROMPT_RESULT_FIELDS}
        return data


def compact_observations(history: list[Observation], last_n: int = 5) -> list[dict[str, Any]]:
    """取最近 N 条观察的精简表示，用于拼 prompt。"""
    return [o.to_prompt_dict() for o in history[-last_n:]]


@dataclass(frozen=True)
class ObservationEpoch:
    """一次**决策所依据的那一屏**（V3.1 P1-6：Observation Epoch / TOCTOU 防护）。

    `DeviceSession.generation` 只回答「Shadow 自己有没有动过设备」——它拦不住
    用户手动点击、通知栏弹出、App 异步刷新、另一个 adb client 的操作。于是会出现：

        观察到 generation=10 的 A 屏 → 模型想了几秒 → 用户自己点了按钮 → B 屏
            ↓
        仍然按 A 屏算出来的坐标去 dispatch        ← TOCTOU

    所以决策必须记住**它依据的是哪一屏**，而不是只记住一个设备计数器。这里把
    「设备代次 + 页面身份（package/activity）+ UI 结构指纹 + 采集时刻」一起快照，
    执行前再比一次：对不上就宁可重新观察，也不拿旧坐标去点新页面。
    """

    generation: int
    package: str = ""
    activity: str = ""
    structure: str = ""
    """UI 树的结构指纹。空字符串表示这次采集没拿到 UI 树（不构成可比较的证据）。"""

    at: float = 0.0

    @classmethod
    def capture(cls, observation, generation: int, *, at: float | None = None) -> "ObservationEpoch":
        """从一次观察里取快照。UI 树为空时 `structure` 留空——不伪造证据。"""
        ui_tree = getattr(observation, "ui_tree", None) or ""
        return cls(
            generation=generation,
            package=getattr(observation, "package", "") or "",
            activity=getattr(observation, "activity", "") or "",
            structure=_structure_digest(ui_tree),
            at=time.time() if at is None else at,
        )

    def stale_reason(self, other: "ObservationEpoch") -> str | None:
        """与执行前重新取到的快照比对，返回「为什么不能按旧决策执行」。

        判据刻意分档，因为三件事的严重程度不同：
        - **设备代次变了**：Shadow 自己在这中间动过设备（或发生了抢占交接），
          这是确定性的信号，一定不能继续；
        - **页面身份变了**（package/activity 不同）：已经换屏了，旧坐标无从谈起；
        - **结构指纹变了**：还在同一个 Activity 里，但界面内容变了——可能只是列表
          滚动，也可能是弹窗盖上来。这类只提示、不硬拦（见 `_toctou_guard`）。
        """
        if self.generation != other.generation:
            return f"设备代次已从 {self.generation} 变为 {other.generation}"
        if (self.package, self.activity) != (other.package, other.activity):
            return (
                f"页面已从 {self.package}/{self.activity} "
                f"变为 {other.package}/{other.activity}"
            )
        if self.structure and other.structure and self.structure != other.structure:
            return "同一页面内 UI 结构已变化（可能被弹窗 / 外部操作覆盖）"
        return None


def _structure_digest(ui_tree: str) -> str:
    """UI 树的结构指纹：只保留标签与 resource-id，丢掉文本与坐标。

    丢文本是刻意的——时钟、电量、未读数这类每秒都在变的文本会让指纹永远不等，
    那这个检查就会退化成「每次都判 stale」。我们要抓的是**结构**变没变。
    """
    if not ui_tree:
        return ""
    parts = re.findall(r'(?:resource-id|class)="([^"]*)"', ui_tree)
    if not parts:
        # 没有可提取的属性就退回长度 + 前 512 字符的摘要，聊胜于无
        parts = [ui_tree[:512]]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
