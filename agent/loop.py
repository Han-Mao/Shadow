"""Observe → Think → Act → Verify 主循环，维护 AgentState，执行重试与熔断。不理解页面，不碰设备。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from device.adb import AdbController
from models.action import Action, ActionType
from models.state import AgentState, Observation, StepStatus
from models.task import TaskStatus

from . import executor, observer, planner, verifier
from .memory import Memory

ARTIFACT_DIR = Path("artifacts/shots")
MAX_RETRY_COUNT = 3


class AgentLoop:
    def __init__(self, adb: AdbController, memory: Memory | None = None) -> None:
        self.adb = adb
        self.memory = memory or Memory()

    def run(self, state: AgentState, max_steps: int | None = None) -> AgentState:
        """运行完整任务循环，直到完成、失败或达到最大步数。"""
        if state.task.status == TaskStatus.PENDING:
            state.task.mark(TaskStatus.RUNNING)
            self._generate_plan(state)

        max_steps = max_steps or state.task.max_steps

        while state.current_step < max_steps:
            state.current_step += 1
            observation = self._step(state)
            self.memory.append(state.task.id, observation)

            if observation.status == StepStatus.DONE:
                state.task.mark(TaskStatus.DONE)
                state.retry_count = 0
                return state

            if observation.status == StepStatus.ERROR:
                state.retry_count += 1
                if state.retry_count >= MAX_RETRY_COUNT:
                    state.task.mark(TaskStatus.FAILED)
                    return state
            else:
                state.retry_count = 0

        state.task.mark(TaskStatus.FAILED)
        return state

    def observe_once(self, state: AgentState) -> Observation:
        """单步观察，用于 POST /observe。"""
        state.current_step += 1
        observation = observer.observe(self.adb, ARTIFACT_DIR, state.current_step)
        self.memory.append(state.task.id, observation)
        return observation

    def execute_action(self, state: AgentState, action_type: str, target: Any = None, value: str | None = None) -> Observation:
        """执行单个 Action 并验证，用于 POST /actions。"""
        state.current_step += 1
        pre_obs = observer.observe(self.adb, ARTIFACT_DIR, state.current_step)
        action = Action(type=ActionType(action_type), target=target, value=value)
        result = executor.execute(self.adb, action, pre_obs.ui_tree)
        post_obs = self._verify_result(state, pre_obs, action, result)
        self.memory.append(state.task.id, post_obs)
        return post_obs

    def _verify_result(
        self,
        state: AgentState,
        pre_obs: Observation,
        action: Action,
        result: dict,
    ) -> Observation:
        """执行成功后重新观测并交由 VLM 验证，返回后置 Observation。"""
        post_obs = observer.observe(self.adb, ARTIFACT_DIR, state.current_step, suffix="post")
        post_obs.action = action
        post_obs.result = result
        status, message = verifier.verify_action(
            state.task.instruction,
            pre_obs,
            action,
            post_obs,
        )
        post_obs.status = status
        post_obs.message = message
        return post_obs

    def _generate_plan(self, state: AgentState) -> None:
        try:
            initial_obs = observer.observe(self.adb, ARTIFACT_DIR, step=0)
            state.task.plan = planner.generate_plan(
                state.task.instruction,
                initial_obs.screenshot_path,
                initial_obs.ui_tree,
            )
        except Exception:
            state.task.plan = []

    def _step(self, state: AgentState) -> Observation:
        pre_obs = observer.observe(self.adb, ARTIFACT_DIR, state.current_step)
        history = state.compact_history(last_n=5)

        try:
            action = planner.plan_next_action(
                state.task.instruction,
                pre_obs.screenshot_path,
                pre_obs.ui_tree,
                history,
                state.task.plan,
            )
        except Exception as exc:
            return self._replan_or_fail(state, pre_obs, f"规划失败: {exc}")

        if action.type == ActionType.DONE:
            pre_obs.action = action
            pre_obs.status = StepStatus.DONE
            pre_obs.message = action.reason or "任务完成"
            return pre_obs

        result = executor.execute(self.adb, action, pre_obs.ui_tree)
        if not result.get("ok"):
            return self._replan_or_fail(state, pre_obs, result.get("error", "执行失败"))

        return self._verify_result(state, pre_obs, action, result)

    def _replan_or_fail(self, state: AgentState, obs: Observation, error: str) -> Observation:
        """规划或执行失败时尝试 Re-plan 一次。"""
        try:
            history = state.compact_history(last_n=5)
            history.append(obs.model_dump(exclude={"ui_tree"}))
            action = planner.replan(
                state.task.instruction,
                obs.screenshot_path,
                obs.ui_tree,
                history,
                error,
            )
        except Exception:
            obs.status = StepStatus.ERROR
            obs.message = error
            return obs

        if action.type == ActionType.DONE:
            obs.action = action
            obs.status = StepStatus.DONE
            obs.message = action.reason or "任务完成"
            return obs

        result = executor.execute(self.adb, action, obs.ui_tree)
        obs.action = action
        obs.result = result
        obs.status = StepStatus.OK if result.get("ok") else StepStatus.ERROR
        obs.message = f"Re-plan 重试: {result if result.get('ok') else result.get('error', '失败')}"
        return obs
