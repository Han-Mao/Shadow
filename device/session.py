"""DeviceSession（V2 §十三）：设备所有权与会话。

原来的 `_DEVICE_LOCK` 只表达「别同时点」，拿不到锁就 409。
DeviceSession 进一步回答三个问题：**谁在占用设备、能不能被抢占、抢占后怎么交接**。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class DeviceBusyError(RuntimeError):
    """设备被别的任务占用。"""


class DeviceSession:
    """单设备的会话所有权。

    所有权用一把 Lock 表达：拿到锁 = 持有设备；`owner` 只是给状态接口看的注解。
    """

    def __init__(self, controller: Any, serial: str = "") -> None:
        self._controller = controller
        self._serial = serial
        self._device_lock = threading.Lock()
        self._meta_lock = threading.RLock()
        self._owner: str | None = None
        self._preempt_for: str | None = None

    # ---- 只读状态 ----

    @property
    def controller(self) -> Any:
        return self._controller

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def owner(self) -> str | None:
        with self._meta_lock:
            return self._owner

    @property
    def busy(self) -> bool:
        return self.owner is not None

    def owned_by(self, task_id: str) -> bool:
        return self.owner == task_id

    def snapshot(self) -> dict[str, Any]:
        with self._meta_lock:
            return {
                "serial": self._serial,
                "owner": self._owner,
                "busy": self._owner is not None,
                "preempt_requested_for": self._preempt_for,
            }

    # ---- 所有权 ----

    def acquire(self, task_id: str, timeout: float = 0.0) -> bool:
        """取得设备。timeout=0 表示不等待，拿不到立刻返回 False。"""
        acquired = (
            self._device_lock.acquire(timeout=timeout)
            if timeout > 0
            else self._device_lock.acquire(blocking=False)
        )
        if acquired:
            with self._meta_lock:
                self._owner = task_id
                self._preempt_for = None
            logger.debug("任务 %s 取得设备 %s", task_id, self._serial)
        return acquired

    def release(self, task_id: str) -> bool:
        """交还设备。

        非持有者调用只记警告并返回 False —— 释放动作经常写在 finally 里，
        这里抛异常会把真正的失败原因盖掉。
        """
        with self._meta_lock:
            if self._owner != task_id:
                logger.warning(
                    "任务 %s 试图释放设备，但当前持有者是 %s，已忽略", task_id, self._owner
                )
                return False
            self._owner = None
            self._preempt_for = None
        self._device_lock.release()
        logger.debug("任务 %s 交还设备 %s", task_id, self._serial)
        return True

    @contextmanager
    def owned(self, task_id: str) -> Iterator[Any]:
        """在执行动作前确认自己仍持有设备。

        抢占是异步发生的：Scheduler 只会给当前持有者打标记，真正让出发生在
        持有者自己的安全点上。因此每一步执行前都要复查一次所有权。
        """
        if not self.owned_by(task_id):
            raise DeviceBusyError(f"任务 {task_id} 当前不持有设备（持有者：{self.owner}）")
        yield self._controller

    # ---- 抢占 ----

    def request_preempt(self, by_task_id: str) -> bool:
        """请求当前持有者让出设备（不强制中断，等它在安全点交接）。"""
        with self._meta_lock:
            if self._owner is None:
                return False
            if self._preempt_for == by_task_id:
                return True  # 同一个等待方重复请求，不必再记一遍
            self._preempt_for = by_task_id
            logger.info("请求任务 %s 让出设备，等待方 %s", self._owner, by_task_id)
            return True

    def should_yield(self, task_id: str) -> bool:
        """当前持有者是否应该让出（供 runtime 在循环安全点检查）。"""
        with self._meta_lock:
            return self._owner == task_id and self._preempt_for is not None

    def clear_preempt(self) -> None:
        with self._meta_lock:
            self._preempt_for = None
