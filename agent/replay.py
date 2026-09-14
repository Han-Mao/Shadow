"""任务回放（V2.1 §二十三）。

`EventLog` 是只追加的事实流，Replay 就是把它按时间轴读回来，
回答调试 Agent 时最常问的那个问题：**它当时到底干了什么、为什么这么干？**

刻意分两层，因为风险差了几个数量级：

- **观测回放**（默认，纯读）：事件流 → 时间轴 → 报告 + 「值得注意的地方」。
  不碰设备、不改状态，随时可跑。
- **动作重放**（默认 `dry_run=True`）：把动作序列提取出来供人工核对，
  或作为将来回归测试的素材。**默认绝不真的执行**——
  手机上的动作有真实副作用（付款、发消息），盲目重放一个「提交订单」
  比不重放危险得多。

数据源必须是 `EventLog`，不能是 `TrajectoryStore`：后者是内存态、会被裁剪，
进程一重启就没了；而回放最被需要的时刻，恰恰是任务失败或崩溃之后。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from storage.event_log import (
    ACTION_DISPATCHED,
    ACTION_VERIFIED,
    CANCELLED,
    CHECKPOINT_SAVED,
    CONFIRMED,
    CREATED,
    DONE,
    EFFECT_UNKNOWN,
    FAILED,
    GOAL_CONFIRMED,
    GOAL_REJECTED,
    GOAL_REQUESTED,
    PREEMPT_REQUESTED,
    QUEUED,
    RECONCILED,
    RECOVERED,
    RESUMED,
    RISK_ASSESSED,
    STARTED,
    SUSPENDED,
    WAITING,
    Event,
    EventLog,
)

logger = logging.getLogger(__name__)


class ReplayPhase(str, Enum):
    """回放帧的归属阶段。分组是为了让人能按「发生了哪类事」跳着看。"""

    LIFECYCLE = "lifecycle"
    """任务级状态流转：排队、启动、挂起、恢复、结束。"""

    ACTION = "action"
    """设备动作：发出、验证。"""

    RECOVERY = "recovery"
    """恢复相关：保存恢复点、动作对账。"""

    CONTROL = "control"
    """人工干预：等待确认、确认/否决。"""


_PHASE_BY_KIND: dict[str, ReplayPhase] = {
    CREATED: ReplayPhase.LIFECYCLE,
    QUEUED: ReplayPhase.LIFECYCLE,
    STARTED: ReplayPhase.LIFECYCLE,
    PREEMPT_REQUESTED: ReplayPhase.LIFECYCLE,
    SUSPENDED: ReplayPhase.LIFECYCLE,
    RESUMED: ReplayPhase.LIFECYCLE,
    RECOVERED: ReplayPhase.LIFECYCLE,
    DONE: ReplayPhase.LIFECYCLE,
    FAILED: ReplayPhase.LIFECYCLE,
    CANCELLED: ReplayPhase.LIFECYCLE,
    ACTION_DISPATCHED: ReplayPhase.ACTION,
    ACTION_VERIFIED: ReplayPhase.ACTION,
    CHECKPOINT_SAVED: ReplayPhase.RECOVERY,
    RECONCILED: ReplayPhase.RECOVERY,
    EFFECT_UNKNOWN: ReplayPhase.RECOVERY,
    RISK_ASSESSED: ReplayPhase.CONTROL,
    GOAL_REQUESTED: ReplayPhase.CONTROL,
    GOAL_CONFIRMED: ReplayPhase.CONTROL,
    GOAL_REJECTED: ReplayPhase.CONTROL,
    WAITING: ReplayPhase.CONTROL,
    CONFIRMED: ReplayPhase.CONTROL,
}

# 「不顺利」的事件。回放报告的第一节就是它们——
# 一个跑成功的任务没什么好看的，出问题的那几帧才是。
_NOTABLE_KINDS = frozenset(
    {
        PREEMPT_REQUESTED,
        SUSPENDED,
        RECONCILED,
        FAILED,
        CANCELLED,
        WAITING,
        RECOVERED,
        # V2.2：效果未知与完成被驳回，都是「系统差点做错但被拦下」的证据
        EFFECT_UNKNOWN,
        GOAL_REJECTED,
    }
)

_PHASE_LABEL = {
    ReplayPhase.LIFECYCLE: "生命周期",
    ReplayPhase.ACTION: "动作",
    ReplayPhase.RECOVERY: "恢复",
    ReplayPhase.CONTROL: "人工",
}


# ---------------------------------------------------------------- 时间轴


@dataclass(frozen=True)
class ReplayFrame:
    """时间轴上的一个事件。"""

    index: int
    offset_seconds: float
    at: str
    kind: str
    phase: ReplayPhase
    summary: str
    data: dict
    notable: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "offset_seconds": round(self.offset_seconds, 3),
            "at": self.at,
            "kind": self.kind,
            "phase": self.phase.value,
            "summary": self.summary,
            "notable": self.notable,
            "data": self.data,
        }


@dataclass
class ReplayTimeline:
    """一个任务的完整回放。"""

    task_id: str
    frames: list[ReplayFrame] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.frames

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for frame in self.frames:
            result[frame.kind] = result.get(frame.kind, 0) + 1
        return result

    def anomalies(self) -> list[ReplayFrame]:
        """值得注意的帧（抢占、对账、失败、等待确认…）。"""
        return [frame for frame in self.frames if frame.notable]

    def actions(self) -> list[ReplayFrame]:
        return [frame for frame in self.frames if frame.kind == ACTION_DISPATCHED]

    def verifications(self) -> list[ReplayFrame]:
        return [frame for frame in self.frames if frame.kind == ACTION_VERIFIED]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "duration_seconds": round(self.duration_seconds, 3),
            "counts": self.counts(),
            "frames": [frame.to_dict() for frame in self.frames],
        }

    def render_markdown(self) -> str:
        """渲染成人读的报告。这是回放最主要的用法：贴到 issue / 群里排查。"""
        if self.empty:
            return f"# 任务回放 `{self.task_id}`\n\n> 没有事件记录。任务可能还没开始跑，或事件目录不对。\n"

        counts = self.counts()
        actions = len(self.actions())
        dangerous = sum(1 for step in build_plan(self).steps if step.dangerous)

        lines = [
            f"# 任务回放 `{self.task_id}`",
            "",
            f"- 事件数：{len(self.frames)}",
            f"- 时长：{self.duration_seconds:.1f}s",
            f"- 动作：{actions}（其中危险动作 {dangerous}）",
            f"- 事件分布：{', '.join(f'{k}×{v}' for k, v in sorted(counts.items()))}",
            "",
        ]

        anomalies = self.anomalies()
        lines.append("## 值得注意的地方")
        lines.append("")
        if anomalies:
            for order, frame in enumerate(anomalies, start=1):
                lines.append(
                    f"{order}. `t+{frame.offset_seconds:.3f}s` **{frame.kind}** —— {frame.summary}"
                )
        else:
            lines.append("（没有异常事件，任务一路顺利）")
        lines.append("")

        lines.append("## 时间轴")
        lines.append("")
        lines.append("| 偏移 | 阶段 | 事件 | 摘要 |")
        lines.append("| --- | --- | --- | --- |")
        for frame in self.frames:
            marker = " ⚠️" if frame.notable else ""
            lines.append(
                f"| `+{frame.offset_seconds:.3f}s` | {_PHASE_LABEL[frame.phase]} "
                f"| {frame.kind}{marker} | {frame.summary} |"
            )
        lines.append("")
        return "\n".join(lines)


def _parse_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_target(target: object) -> str:
    if isinstance(target, dict) and "x" in target and "y" in target:
        return f"({target['x']:g},{target['y']:g})"
    if target is None:
        return ""
    if isinstance(target, (dict, list)):
        return json.dumps(target, ensure_ascii=False)
    return str(target)


def _summarize(kind: str, data: dict) -> str:
    """把一条事件翻译成一句人话。

    刻意不追求覆盖所有字段——摘要要能一眼看懂，细节留在 data 里给程序用。
    """
    if kind == STARTED:
        return f"开始执行（v{data.get('version')}）：{data.get('instruction', '')}"
    if kind == QUEUED:
        return f"进入队列（优先级 {data.get('priority')}）"
    if kind == ACTION_DISPATCHED:
        target = _format_target(data.get("target"))
        value = data.get("value")
        detail = f" → {target}" if target else ""
        if value not in (None, ""):
            detail += f" 输入 {value!r}"
        risk = data.get("risk")
        risk_note = f"［{risk}］" if risk and risk != "safe" else ""
        return f"执行 {data.get('action')}{detail}{risk_note}"
    if kind == ACTION_VERIFIED:
        return (
            f"验证 {data.get('outcome')}：效果 {data.get('effect')}"
            f"（证据来自 {data.get('layer') or '未知'}）"
            + (f" —— {data['message']}" if data.get("message") else "")
        )
    if kind == CHECKPOINT_SAVED:
        return f"保存恢复点 {str(data.get('checkpoint_id', ''))[:24]}（动作效果 {data.get('effect')}）"
    if kind == PREEMPT_REQUESTED:
        return (
            f"被 {data.get('by_task_id')} 抢占"
            f"（{data.get('by_priority')} > {data.get('running_priority')}）"
        )
    if kind == SUSPENDED:
        return f"让出设备（原因 {data.get('reason')}，已执行 {data.get('step')} 步）"
    if kind == RESUMED:
        return "恢复执行"
    if kind == RECOVERED:
        return f"重启后恢复（{data.get('restored_as')}）"
    if kind == RECONCILED:
        return f"动作对账 → {data.get('verdict')}：{data.get('reason')}"
    if kind == EFFECT_UNKNOWN:
        return (
            f"效果未知：{data.get('action')} 已发出但拿不到验证观察（{data.get('reason')}）"
        )
    if kind == RISK_ASSESSED:
        note = "（模型试图降级被拒）" if data.get("downgrade_blocked") else ""
        return f"风险判定 {data.get('effective')}{note}：{data.get('reason')}"
    if kind == GOAL_REQUESTED:
        return f"模型申请完成（第 {data.get('rejections', 0)} 次被驳回过）：{data.get('reason')}"
    if kind == GOAL_CONFIRMED:
        return f"目标验证通过（{data.get('layer')}）：{data.get('reason')}"
    if kind == GOAL_REJECTED:
        return f"完成申请被**驳回**：{data.get('reason')}"
    if kind == WAITING:
        return f"命中危险动作 {data.get('action')}（{data.get('risk')}），等待人工确认"
    if kind == CONFIRMED:
        verdict = "批准" if data.get("approved") else "否决"
        return f"人工{verdict}：{data.get('action', '')}"
    if kind == DONE:
        return f"完成（共 {data.get('steps')} 步）"
    if kind == FAILED:
        return f"失败：{data.get('reason')}"
    if kind == CANCELLED:
        return "任务被取消"
    return kind


def build_timeline(task_id: str, events: list[Event]) -> ReplayTimeline:
    """把事件列表转成时间轴。"""
    frames: list[ReplayFrame] = []
    origin: datetime | None = None
    last_offset = 0.0

    for event in events:
        moment = _parse_at(event.at)
        if moment is not None and origin is None:
            origin = moment
        # 时间戳缺失或倒挂时沿用上一个偏移，保证时间轴单调——
        # 回放的价值在于顺序，不该因为一条坏数据就整段错位
        offset = last_offset
        if moment is not None and origin is not None:
            offset = max(0.0, (moment - origin).total_seconds())
        last_offset = offset

        notable = event.kind in _NOTABLE_KINDS or (
            event.kind == ACTION_VERIFIED and event.data.get("outcome") == "error"
        )
        frames.append(
            ReplayFrame(
                index=len(frames),
                offset_seconds=offset,
                at=event.at,
                kind=event.kind,
                phase=_PHASE_BY_KIND.get(event.kind, ReplayPhase.LIFECYCLE),
                summary=_summarize(event.kind, event.data),
                data=event.data,
                notable=notable,
            )
        )

    return ReplayTimeline(task_id=task_id, frames=frames, duration_seconds=last_offset)


def load_timeline(event_log: EventLog, task_id: str, *, limit: int = 1000) -> ReplayTimeline:
    """从事件日志直接读出一个任务的时间轴。"""
    return build_timeline(task_id, event_log.read(task_id, limit=limit))


# ---------------------------------------------------------------- 动作重放


class ReplayRefused(RuntimeError):
    """拒绝重放——通常是没显式放行危险动作。"""


@dataclass(frozen=True)
class ReplayStep:
    """从事件流里还原出来的一个动作。"""

    index: int
    kind: str
    action: str
    target: object = None
    value: object = None
    risk: str = "safe"
    attempt_id: str | None = None
    execution_step: int | None = None
    at: str = ""

    @property
    def dangerous(self) -> bool:
        return self.risk == "dangerous"

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "action": self.action,
            "target": self.target,
            "value": self.value,
            "risk": self.risk,
            "attempt_id": self.attempt_id,
            "execution_step": self.execution_step,
            "at": self.at,
            "dangerous": self.dangerous,
        }

    def describe(self) -> str:
        target = _format_target(self.target)
        detail = f" → {target}" if target else ""
        return f"#{self.index} {self.action}{detail}" + (f" 输入 {self.value!r}" if self.value else "")


@dataclass
class ReplayPlan:
    """动作序列。给人工核对，或作为回归测试的素材。"""

    task_id: str
    steps: list[ReplayStep] = field(default_factory=list)

    def dangerous_steps(self) -> list[ReplayStep]:
        return [step for step in self.steps if step.dangerous]

    def summary(self) -> str:
        dangerous = len(self.dangerous_steps())
        tail = f"，其中 {dangerous} 个危险动作" if dangerous else ""
        return f"{len(self.steps)} 个动作{tail}"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "count": len(self.steps),
            "dangerous_count": len(self.dangerous_steps()),
            "steps": [step.to_dict() for step in self.steps],
        }


def build_plan(timeline: ReplayTimeline) -> ReplayPlan:
    """从时间轴里提取动作序列（只取「发出」的事件，验证不产生新动作）。"""
    steps: list[ReplayStep] = []
    for frame in timeline.frames:
        if frame.kind != ACTION_DISPATCHED:
            continue
        data = frame.data
        steps.append(
            ReplayStep(
                index=len(steps),
                kind=frame.kind,
                action=str(data.get("action", "")),
                target=data.get("target"),
                value=data.get("value"),
                risk=str(data.get("risk", "safe")),
                attempt_id=data.get("attempt_id"),
                execution_step=data.get("step"),
                at=frame.at,
            )
        )
    return ReplayPlan(task_id=timeline.task_id, steps=steps)


@dataclass
class ReplayResult:
    task_id: str
    dry_run: bool
    executed: int = 0
    plan: ReplayPlan | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "dry_run": self.dry_run,
            "executed": self.executed,
            "note": self.note,
            "plan": self.plan.to_dict() if self.plan else None,
        }


def replay(
    event_log: EventLog,
    task_id: str,
    *,
    dry_run: bool = True,
    execute=None,
    allow_dangerous: bool = False,
) -> ReplayResult:
    """重放一个任务的动作序列。

    默认只做 dry-run（列出将要重放的动作，一个都不执行）。
    真要执行必须同时满足：显式 `dry_run=False` + 传入 `execute` 回调 +
    （若含危险动作）`allow_dangerous=True`。

    **即便都满足，也应该只在离线回归测试里用**：重放前必须自己确保设备处在
    对应的恢复点状态，否则页面上下文对不上，重放出来的结果没有参考价值。
    生产环境想复现问题，请用观测回放（`load_timeline`）+ 恢复点，而不是重放动作。
    """
    timeline = load_timeline(event_log, task_id)
    plan = build_plan(timeline)

    if dry_run:
        return ReplayResult(
            task_id=task_id,
            dry_run=True,
            plan=plan,
            note=f"dry-run：{plan.summary()}，未执行任何动作",
        )

    dangerous = plan.dangerous_steps()
    if dangerous and not allow_dangerous:
        raise ReplayRefused(
            f"计划里含 {len(dangerous)} 个危险动作"
            f"（{', '.join(step.describe() for step in dangerous[:3])}），"
            "默认拒绝重放。确认要执行请显式传 allow_dangerous=True。"
        )
    if execute is None:
        raise ReplayRefused("dry_run=False 时必须提供 execute 回调")

    executed = 0
    for step in plan.steps:
        execute(step)
        executed += 1
    return ReplayResult(
        task_id=task_id,
        dry_run=False,
        executed=executed,
        plan=plan,
        note="已重放全部动作",
    )
