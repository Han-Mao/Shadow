"""Observe → Think → Act → Verify 主循环，维护 AgentState，执行重试与熔断。不理解页面，不碰设备。"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from device.adb import AdbController
from models.action import Action, ActionType
from models.state import AgentState, Observation, StepStatus
from models.task import TaskStatus

from . import executor, observer, planner, verifier
from .memory import Memory

logger = logging.getLogger(__name__)

# 与 api/server.py 读同一个环境变量，否则 /screenshot 与 Agent 截图会落到不同目录
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts/shots"))
MAX_RETRY_COUNT = 3


class AgentLoop:
    def __init__(self, adb: AdbController, memory: Memory | None = None) -> None:
        self.adb = adb
        self.memory = memory or Memory()

    def run(self, state: AgentState, max_steps: int | None = None) -> AgentState:
        """运行完整任务循环，直到完成、失败或达到最大步数。

        异常安全：任务一旦进入 RUNNING，任何未预期异常都必须先把状态落为 FAILED
        再向上抛——否则任务会永远卡在 RUNNING，且无法从 /tasks/{id} 看出它已经死了。
        """
        # 幂等注册：调用方忘记 memory.put 时不应在 append 处抛 KeyError 中断任务
        self.memory.put(state)
        try:
            return self._drive(state, max_steps)
        except Exception:
            state.task.mark(TaskStatus.FAILED)
            logger.exception("任务 %s 异常中断，状态已置为 failed", state.task.id)
            raise

    def _drive(self, state: AgentState, max_steps: int | None) -> AgentState:
        """主循环本体。"""
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
        self.memory.put(state)
        state.current_step += 1
        observation = observer.observe(self.adb, ARTIFACT_DIR, state.current_step)
        self.memory.append(state.task.id, observation)
        return observation

    def execute_action(self, state: AgentState, action_type: str, target: Any = None, value: str | None = None) -> Observation:
        """执行单个 Action 并验证，用于 POST /actions。"""
        self.memory.put(state)
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
        try:
            post_obs = observer.observe(self.adb, ARTIFACT_DIR, state.current_step, suffix="post")
        except Exception as exc:
            # 动作已经执行成功了，只是拿不到新截图：降级为 OK 并如实说明未验证。
            # 若这里抛异常，一次设备抖动就会触发无谓的 Re-plan（重发一次动作）。
            logger.warning("第 %s 步后置观察失败，本步降级为未验证: %s", state.current_step, exc)
            pre_obs.action = action
            pre_obs.result = result
            pre_obs.status = StepStatus.OK
            pre_obs.message = f"执行成功，但重新观察失败（未验证）: {exc}"
            return pre_obs

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
        """基于首屏生成计划。

        step=0 的这张截图会写进 history：它已经落盘，若不入 history 就成了
        「文件存在但 /tasks/{id}/shots/0 取不到」的幽灵记录。
        """
        try:
            initial_obs = observer.observe(self.adb, ARTIFACT_DIR, step=0)
        except Exception as exc:
            logger.warning("初始观察失败，跳过计划生成: %s", exc)
            state.task.plan = []
            return

        self.memory.append(state.task.id, initial_obs)

        try:
            state.task.plan = planner.generate_plan(
                state.task.instruction,
                initial_obs.screenshot_path,
                initial_obs.ui_tree,
            )
        except Exception as exc:
            # 计划只是提示，失败不阻塞执行；但要留下日志，否则 VLM 配置错误完全静默
            logger.warning("生成计划失败，将按无计划执行: %s", exc)
            state.task.plan = []

    def _step(self, state: AgentState) -> Observation:
        try:
            pre_obs = observer.observe(self.adb, ARTIFACT_DIR, state.current_step)
        except Exception as exc:
            # 观察失败多半是设备瞬时抖动（截图 / dump / focus 查询都是子进程）。
            # 这里不做 Re-plan —— Re-plan 同样需要截图，此刻必然也失败；
            # 直接记为 ERROR，交给上层 retry_count 熔断，重试时自然会重新观察。
            logger.warning("第 %s 步观察失败: %s", state.current_step, exc)
            return Observation(
                step=state.current_step,
                screenshot_path="",
                status=StepStatus.ERROR,
                message=f"观察失败: {exc}",
            )

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
            pre_obs.result = {"ok": True, "done": True}
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
            history.append(obs.to_prompt_dict())
            action = planner.replan(
                state.task.instruction,
                obs.screenshot_path,
                obs.ui_tree,
                history,
                error,
            )
        except Exception as exc:
            logger.warning("Re-plan 失败，本步记为错误: %s", exc)
            obs.status = StepStatus.ERROR
            obs.message = error
            return obs

        if action.type == ActionType.DONE:
            obs.action = action
            obs.result = {"ok": True, "done": True}
            obs.status = StepStatus.DONE
            obs.message = action.reason or "任务完成"
            return obs

        result = executor.execute(self.adb, action, obs.ui_tree)
        if not result.get("ok"):
            obs.action = action
            obs.result = result
            obs.status = StepStatus.ERROR
            obs.message = f"Re-plan 重试后仍失败: {result.get('error', '失败')}"
            return obs

        # 重试路径也必须过 VLM 验证：否则「执行返回 ok」和「页面确实变了」两套标准
        # 会让统计里混进一批名义成功、实际没生效的步骤
        logger.info("Re-plan 重试已执行，交由 VLM 验证")
        return self._verify_result(state, obs, action, result)
