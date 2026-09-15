"""启动恢复：进程被杀后留下的非终态执行（v4.1 §六/§七）。

## 为什么必须有这一层

执行记录是「手机被操作过」的唯一凭据，而进程可以被 `kill -9`、被低内存杀掉、
被用户强停。崩溃时库里会留下一条**非终态**的记录：从 `GET /executions` 看像是
「还在执行」，实际上没有任何进程会再去推进它。这种幽灵记录比「失败」危险得多——
失败会让人去查，幽灵只会让人以为没事。

## 两条出路，判据只有一条

    CREATED / RISK_CHECKED / DISPATCHED  → FAILED   设备从未被调用，动作确定没发生
    RUNNING                              → UNKNOWN  可能已经产生副作用，禁止自动重试

分界就是 `executor.execute` 有没有被调用过（见 `state.recovery_target` 的说明）。
`UNKNOWN` **不是**「保守一点的失败」：它意味着「手机那边可能已经付款了」，
所以它在 `state.LEGAL_TRANSITIONS` 里没有出边——没有任何代码路径能把它改回
可以重试的状态。想继续只有一条路：重新观察手机、对账
（`agent/reconciliation`），或者问人。

## 宽限窗口（`stale_after`）

默认 `0`：**启动时看到非终态执行，它就是死进程留下的**。这个结论成立的前提是
「单进程」——而单进程在本仓库是**硬前提**（`api/server.py` 的 `_guard_single_process()`
在检测到多 worker 时直接拒绝启动，约定 [78]）。

显式开了 `SHADOW_ALLOW_MULTI_PROCESS` 的部署就不是这个前提了：另一个进程可能
正拿着某条 RUNNING 在执行。那种情况下把宽限设成 300 秒（审核给的那个值）——
比它更年轻的记录留给可能还活着的持有者，超时了才轮到本进程接管。

宽限的值由**调用方**（`api/server.py`）决定并传进来：那里才知道这次部署是不是
多进程。本模块只负责「按给定时限做恢复」。
"""
from __future__ import annotations

import logging
from datetime import datetime

from models.execution import ExecutionStatus

from . import state

logger = logging.getLogger(__name__)

# 恢复理由的文案要写清「凭什么这么判」——它是执行记录的 `note`，
# 事后查这条记录的人只会看到这一句话。
_NOTE_FAILED = (
    "启动恢复：进程在动作交给设备之前终止，设备从未被调用，"
    "判定为 FAILED（这个动作如果还有必要，可以安全重做）"
)
_NOTE_UNKNOWN = (
    "启动恢复：进程在动作交给设备之后终止，手机侧可能已经产生副作用，"
    "判定为 UNKNOWN——**禁止自动重试**，只能重新观察对账或转人工确认"
)


def _stamp(record) -> str:
    """判断「多久没动静了」用哪个时间戳。

    `RUNNING` 用 `started_at`（动作真正交给设备的时刻），其余用 `created_at`。
    两者都是**本机写下的**时间戳，所以与恢复时读到的 `now` 在同一个时钟上。
    """
    return record.started_at or record.created_at or ""


def _age_seconds(record, now: datetime) -> float | None:
    """这条记录停在当前状态多久了。时间戳解析不了时返回 None（当作「不知道」）。"""
    raw = _stamp(record)
    if not raw:
        return None
    try:
        return (now - datetime.fromisoformat(raw)).total_seconds()
    except ValueError:
        return None


def recover_executions(
    service,
    *,
    stale_after: float = 0.0,
    now: datetime | None = None,
    limit: int = 500,
) -> dict[str, int]:
    """扫描非终态执行并落定它们，返回各结局的条数。

    幂等：恢复过的记录已是终态，下次启动不会再被选中。

    返回的计数字典（`unknown` / `failed` / `skipped` / `young`）是给启动日志与
    `/health/detail` 看的——「这次启动接管了几条崩溃遗留」必须是可观测的，
    否则「为什么有一条执行变成了 UNKNOWN」只能靠猜。
    """
    counts = {
        ExecutionStatus.UNKNOWN: 0,
        ExecutionStatus.FAILED: 0,
        "skipped": 0,  # 守卫落空：别的进程刚把它推进/落定了
        "young": 0,  # 还在宽限窗口内：可能属于一个活着的进程
    }
    moment = now or datetime.now()

    for record in service.store.unfinished(limit=limit):
        target = state.recovery_target(record.status)
        if target is None:  # pragma: no cover - 查询条件已经排除了终态
            continue

        if stale_after > 0:
            age = _age_seconds(record, moment)
            if age is not None and age < stale_after:
                counts["young"] += 1
                logger.info(
                    "执行 %s（%s）年龄 %.1fs < 宽限 %.0fs，暂不恢复",
                    record.execution_id,
                    record.status,
                    age,
                    stale_after,
                )
                continue

        note = _NOTE_UNKNOWN if target == ExecutionStatus.UNKNOWN else _NOTE_FAILED
        try:
            moved = service.recover(record, target=target, note=note)
        except Exception as exc:  # noqa: BLE001 - 一条恢复失败不挡住其余（也不挡住启动）
            counts["skipped"] += 1
            logger.warning("恢复执行 %s 失败：%s", record.execution_id, exc)
            continue

        if not moved:
            counts["skipped"] += 1
            continue
        counts[target] += 1
        logger.warning(
            "启动恢复：执行 %s（原状态 %s）→ %s",
            record.execution_id,
            record.status,
            target,
        )
        if target == ExecutionStatus.UNKNOWN:
            # 这一条必须显眼：它代表「有一台手机可能已经被操作过，而我们不知道结果」。
            logger.warning(
                "执行 %s 效果未知：手机侧可能已产生副作用，"
                "禁止自动重试，请重新观察对账或人工确认（GET /executions?status=UNKNOWN）",
                record.execution_id,
            )

    return counts
