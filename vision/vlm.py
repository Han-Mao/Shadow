"""VLM 调用：页面理解、计划生成、单步决策、执行验证。不输出 ADB 命令。"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from models.action import COMPLETION_ACTION_TYPES, Action, ActionRisk, ActionType, Decision, Point
from models.task_plan import TaskPlan

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o"
VLM_TIMEOUT_SECONDS = 60.0
VLM_MAX_ATTEMPTS = 3
VLM_BACKOFF_SECONDS = 0.8
# 可重试状态：限流与 5xx 属瞬时故障；其余 4xx（如 401 认证失败）重试没有意义
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# 图片精度：坐标决策必须 high；计划与验证只需判断页面语义，low 足够，能省下大量 token 与延迟
_DEFAULT_IMAGE_DETAIL = {"plan": "low", "decide": "high", "verify": "low"}


def _image_detail(stage: str) -> str:
    """按阶段取图片精度，可用 VLM_DETAIL_PLAN / VLM_DETAIL_DECIDE / VLM_DETAIL_VERIFY 覆盖。"""
    return os.getenv(f"VLM_DETAIL_{stage.upper()}", _DEFAULT_IMAGE_DETAIL[stage])


class VlmError(RuntimeError):
    pass


class VlmParseError(VlmError):
    pass


class VlmVerifyError(VlmError):
    """VLM 给了一个**无法解读**的验证结论。

    单独一个异常类型是有意的（V2.2 §七）：以前 `str(data.get("result", "ok")).lower()`
    会把 `{}` 解析成 `"ok"`、把 `{"result": "banana"}` 解析成 `"banana"`，
    而 verifier 对认不出的值一律按成功处理——等于「模型胡说 = 这步过了」。
    现在它必须是一个明确的错误，由调用方按「证据不足」处理。
    """


class VerifyResult(str, Enum):
    """验证结论的**严格**取值集合（V2.2 §七）。"""

    OK = "ok"
    ERROR = "error"
    DONE = "done"


def _encode_image(path: str | Path) -> str:
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("utf-8")


def _extract_json(text: str) -> dict[str, Any]:
    """从可能包含 markdown 代码块或解释文字的文本中提取 JSON 对象。

    VLM 经常在 JSON 前后附带说明（"好的，我的判断是：{...}"），直接 json.loads 会失败，
    因此按「代码块 → 最外层大括号 → 原文」依次尝试。
    """
    if not isinstance(text, str) or not text.strip():
        raise VlmParseError("VLM 返回内容为空")

    candidates = [m.group(1).strip() for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text)]
    candidates.append(text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    raise VlmParseError(f"无法从 VLM 响应中解析 JSON 对象: {text[:200]!r}")


def _compact_ui_tree(ui_tree: str | None, max_nodes: int = 20) -> str:
    """把 XML 压缩成可点击元素列表，供 VLM 理解结构（路线 B 接入）。"""
    if not ui_tree:
        return ""
    try:
        from . import parser
        root = parser.parse(ui_tree)
        nodes = parser.find_clickable(root)[:max_nodes]
        lines = []
        for i, node in enumerate(nodes, 1):
            label = node.text or node.content_desc or node.resource_id or node.class_name
            x, y = node.center
            lines.append(f"{i}. {label} [{x},{y}]")
        return "\n".join(lines)
    except Exception:
        # 解析失败时返回空串，不要把异常文本当成 UI 结构塞进 prompt 误导模型
        return ""


def build_plan_prompt(instruction: str, screenshot_path: str, ui_tree: str | None) -> str:
    tree = _compact_ui_tree(ui_tree)
    return (
        "你正在控制一部 Android 模拟器。请根据当前页面截图与可点击元素列表，"
        f"为以下任务制定一份执行计划（3-8 步）：\n{instruction}\n\n"
        "可点击元素（部分）：\n" + (tree or "无") + "\n\n"
        "请先复述这次任务的三件事，再给步骤（v4.3 §1：任务级的规划上下文）：\n"
        "  `goal`              —— 这一趟到底要达成什么（一句话）\n"
        "  `constraints`       —— 执行过程中**不许做**什么（数组，没有就给空数组）\n"
        "  `success_condition` —— 什么现象出现才算完成（一句话，可被观察到的）\n"
        "然后给每一步：`goal`（这一步要达成什么）与 "
        "`expected_state`（做完之后页面上应该能看到什么，用于事后核对）。\n"
        "请仅返回 JSON：\n"
        "{\n"
        '  "goal": "打开微信给张三发消息",\n'
        '  "constraints": ["不要跳转到浏览器"],\n'
        '  "success_condition": "消息出现在与张三的聊天里",\n'
        '  "steps": [\n'
        '    {"goal": "打开微信", "expected_state": "微信首页出现"},\n'
        '    {"goal": "进入与张三的聊天", "expected_state": "聊天页标题是张三"}\n'
        "  ]\n"
        "}"
    )


def _format_target(target: Any) -> str:
    if isinstance(target, dict):
        return "(" + ",".join(f"{k}={v}" for k, v in target.items()) + ")"
    return str(target)


def _format_action(action: Any) -> str:
    """把 action 压成一行。

    history 里的 action 是 dict，整块 dump 进 prompt 又长又难读；而 action 为 None
    的观察步（如计划阶段的初始截图）直接显示 "None" 会让模型误以为发生了动作。
    """
    if not action:
        return "（无动作，仅观察）"
    if not isinstance(action, dict):
        return str(action)

    parts = [str(action.get("type") or "?")]
    target = action.get("target")
    if target is not None:
        parts.append(_format_target(target))
    value = action.get("value")
    if value is not None:
        parts.append(f"value={value!r}")
    return " ".join(parts)


def build_decision_prompt(
    instruction: str,
    screenshot_path: str,
    ui_tree: str | None,
    history: list[dict[str, Any]],
    plan: list[str],
    current_step_goal: str = "",
    plan_context: str = "",
) -> str:
    steps = "\n".join(
        f"步骤 {o['step']}: {_format_action(o.get('action'))} -> {o.get('status')} "
        f"({o.get('message') or '无说明'})"
        for o in history[-5:]
    )
    plan_text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(plan)) if plan else "无"
    focus = current_step_goal.strip() or "（尚未确定，请自行判断）"
    tree = _compact_ui_tree(ui_tree)
    # 任务级上下文（目标 / 约束 / 完成条件，v4.3 §1）：**空就不占篇幅**——
    # 「约束：无」这种行只会稀释 prompt，而模型对「没写」和「空」的理解是一样的。
    return (
        "你正在控制一部 Android 模拟器。任务：\n" + instruction + "\n\n"
        + (plan_context + "\n\n" if plan_context else "")
        + "整体计划（[状态] 目标）：\n" + plan_text + "\n\n"
        "当前聚焦步骤：\n" + focus + "\n\n"
        "最近执行记录：\n" + (steps or "无") + "\n\n"
        "当前页面可点击元素（部分）：\n" + (tree or "无") + "\n\n"
        "请以 JSON 格式返回下一步操作，不要包含其他解释：\n"
        "{\n"
        '  "thought": "对当前页面的简短分析",\n'
        '  "action_type": "tap|long_press|type|swipe|back|home|launch|wait|done",\n'
        '  "target": "点击目标的描述或坐标，如 {\"x\":360,\"y\":600} 或 \"搜索按钮\"",\n'
        '  "value": "当 action_type=type 时填写要输入的文本；swipe 时填写 x1,y1,x2,y2；wait 时填写毫秒",\n'
        '  "step_done": false,\n'
        '  "done": false,\n'
        '  "risk_hint": "safe|caution|dangerous（仅建议，最终风险由服务端策略裁定）",\n'
        '  "goal_evidence": {"package": "当前应在的包名", "activity": "当前应在的 Activity", "text": "页面上应出现的文字"}\n'
        "}\n"
        "step_done 表示「做完这个动作后，当前聚焦步骤是否已经达成」；done 表示整个任务是否已经完成。\n"
        "注意：done 只是**申请**完成——服务端会用截图、UI 树与包名独立核验，"
        "核验不通过会把任务打回来继续做。所以声称完成时请把 goal_evidence 填上，"
        "否则这次完成申请会因为没有独立证据而被记为「未验证」。\n"
        "坐标优先返回屏幕绝对像素；若不确定，返回可点击元素的文本或 content-desc 描述。"
    )


def build_replan_prompt(context: dict[str, Any]) -> str:
    """结构化 Re-plan prompt（V2 §十六）。

    明确告诉模型「是这种执行方式失败，不是任务失败」，并列出已经试过的做法，
    否则它极大概率把同一个动作原样重发一遍。
    """
    previous = _format_action(context.get("previous_action"))
    tried = context.get("failed_strategies") or []
    tried_text = "\n".join(f"- {item}" for item in tried) if tried else "（暂无）"
    alternatives = context.get("available_alternatives") or []
    alternatives_text = "\n".join(f"- {item}" for item in alternatives) if alternatives else "（无）"

    return (
        "你正在控制一部 Android 模拟器。**任务本身没有失败**，失败的是下面这种执行方式。\n"
        "请换一种做法，不要重复已经失败过的动作。\n\n"
        f"任务：{context.get('task', '')}\n"
        f"当前步骤：{context.get('current_step', '')}\n"
        f"上一次动作：{previous}\n"
        f"失败原因：{context.get('failure_reason') or '未知'}\n\n"
        "已经失败过的做法：\n" + tried_text + "\n\n"
        "可以尝试的替代手段：\n" + alternatives_text + "\n\n"
        "请以 JSON 格式返回下一步操作：\n"
        "{\n"
        '  "thought": "这次改用哪种定位/路径，以及为什么上次会失败",\n'
        '  "action_type": "tap|long_press|type|swipe|back|home|launch|wait|done",\n'
        '  "target": "{\\"x\\":360,\\"y\\":600} 或元素文本描述",\n'
        '  "value": "type 时填文本；swipe 时填 x1,y1,x2,y2；wait 时填毫秒",\n'
        '  "step_done": false,\n'
        '  "done": false\n'
        "}"
    )


def _post_chat_completions(url: str, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
    """带指数退避的 VLM 请求。

    主循环的重试只作用于 action 维度；VLM 自己若不重试，一次限流或网络抖动就会
    连锁触发无谓的 Re-plan，甚至让整个任务失败。
    """
    last_error = ""
    for attempt in range(1, VLM_MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=VLM_TIMEOUT_SECONDS)
        except httpx.TimeoutException as exc:
            last_error = f"请求超时: {exc}"
        except httpx.TransportError as exc:
            last_error = f"网络错误: {exc}"
        else:
            if resp.status_code not in RETRYABLE_STATUS_CODES:
                return resp
            last_error = f"HTTP {resp.status_code}"

        if attempt < VLM_MAX_ATTEMPTS:
            delay = VLM_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "VLM 请求失败（第 %d/%d 次，%s），%.1fs 后重试",
                attempt,
                VLM_MAX_ATTEMPTS,
                last_error,
                delay,
            )
            time.sleep(delay)

    raise VlmError(f"VLM 请求失败（已重试 {VLM_MAX_ATTEMPTS} 次）: {last_error}")


def _call_vlm(messages: list[dict]) -> dict[str, Any]:
    base_url = os.getenv("VLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.getenv("VLM_API_KEY")
    model = os.getenv("VLM_MODEL", DEFAULT_MODEL)

    if not api_key:
        raise VlmError("未设置 VLM_API_KEY 环境变量")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
    }

    resp = _post_chat_completions(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
    )

    try:
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # 走到这里的是不可重试的 4xx（认证失败、请求非法等），直接暴露给上层
        raise VlmError(f"VLM 请求失败: {exc}") from exc

    try:
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        # 兼容 content 为列表的新型 multimodal 格式，如果第一个块有文本就取它
        if isinstance(message.get("content"), list) and len(message["content"]) > 0:
            for block in message["content"]:
                if block.get("type") == "text":
                    message["content"] = block.get("text", "")
                    break
        # 确保 content 为字符串，否则取空
        if not isinstance(message.get("content"), str):
            message["content"] = ""
        return message
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        # ValueError 覆盖 resp.json() 对非法 JSON 的解码失败，统一收敛为 VlmError
        raise VlmError(f"无法解析 VLM 响应结构: {exc}") from exc


def generate_plan(instruction: str, screenshot_path: str, ui_tree: str | None) -> "TaskPlan":
    """根据首屏生成一份计划（`TaskPlan{goal, constraints, success_condition, steps}`）。

    归一化放在这里而不是让每个调用方自己判类型（v4.2 §三 P1 / v4.3 §1）：
    `Task.set_plan` 要能吃「完整 dict」「纯步骤列表」「TaskPlan」三种形状，
    而那三种形状的解析只该有一份（`TaskPlan.from_payload`）。

    模型不按 prompt 给（只给步骤数组、或干脆只给字符串）时**降级而不是报错**：
    计划缺失只影响提示质量，不该让任务直接失败。
    """
    image_b64 = _encode_image(screenshot_path)
    prompt = build_plan_prompt(instruction, screenshot_path, ui_tree)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": _image_detail("plan")},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
        if isinstance(data, dict):
            return TaskPlan.from_payload(data)
        if isinstance(data, list):
            return TaskPlan.from_payload(data)
    except (VlmError, json.JSONDecodeError, KeyError):
        pass
    return TaskPlan()


def _normalize_plan_item(item: Any) -> dict:
    """计划里的一项 → `{"goal": …, "expected_state": …}`（模型给字符串也认）。

    保留给旧调用点与用例；新代码走 `TaskPlan.from_payload` →
    `models.task_step.build_steps`（同一条归一化路径）。
    """
    if isinstance(item, dict):
        return {
            "goal": str(item.get("goal") or item.get("description") or "").strip(),
            "expected_state": str(item.get("expected_state") or item.get("expect") or "").strip(),
        }
    return {"goal": str(item).strip(), "expected_state": ""}


def decide_next_action(
    instruction: str,
    screenshot_path: str,
    ui_tree: str | None,
    history: list[dict[str, Any]] | None = None,
    plan: list[str] | None = None,
    current_step_goal: str = "",
    plan_context: str = "",
) -> Action:
    """根据截图与任务历史，调用 VLM 返回下一步 Action。

    `plan_context` 是任务级的目标/约束/完成条件（v4.3 §1），
    由 `Task.plan_context_lines()` 渲染——每一步决策都看得到「别跑偏、别越约束」。
    """
    image_b64 = _encode_image(screenshot_path)
    prompt = build_decision_prompt(
        instruction, screenshot_path, ui_tree, history or [], plan or [], current_step_goal,
        plan_context,
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": _image_detail("decide")},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
    except VlmParseError:
        raise
    except (VlmError, json.JSONDecodeError) as exc:
        raise VlmParseError(f"无法解析 VLM 响应: {exc}") from exc

    return _parse_decision(data)


def replan_action(
    context: dict[str, Any],
    screenshot_path: str,
    ui_tree: str | None,
    history: list[dict[str, Any]] | None = None,
) -> Action:
    """结构化 Re-plan：换一种执行方式，而不是重发同一个动作。"""
    image_b64 = _encode_image(screenshot_path)
    prompt = build_replan_prompt(context)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": _image_detail("decide")},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
    except VlmParseError:
        raise
    except (VlmError, json.JSONDecodeError) as exc:
        raise VlmParseError(f"无法解析 Re-plan 响应: {exc}") from exc

    return _parse_decision(data)


def _parse_action(data: dict[str, Any]) -> Action:
    action_type_str = data.get("action_type")
    if not action_type_str:
        raise VlmParseError("VLM 未返回 action_type")

    try:
        action_type = ActionType(action_type_str)
    except ValueError as exc:
        raise VlmParseError(f"VLM 返回未知 action_type: {action_type_str}") from exc

    target = data.get("target")
    if isinstance(target, dict):
        # 保留浮点，归一化坐标(0,1)交由 grounding 依据屏幕尺寸换算，避免 int() 截断成 0
        target = Point(x=target.get("x", 0), y=target.get("y", 0))

    value = data.get("value")
    reason = data.get("thought", "")

    if data.get("done") and action_type not in COMPLETION_ACTION_TYPES:
        action_type = ActionType.DONE

    # 模型的风险表态写进 `risk_hint`（**建议**），而不是 `risk`（权威标注）。
    # 两者在 effective_risk 里等价（都只能抬不能降），但分开存之后，
    # 审计能一眼看出「这级风险是模型说的还是策略判的」（V2.2 §一）。
    hint = data.get("risk_hint", data.get("risk"))
    risk_hint = hint if hint in ("safe", "caution", "dangerous") else None

    return Action(
        type=action_type,
        target=target,
        value=value,
        reason=reason,
        risk_hint=ActionRisk(risk_hint) if risk_hint else None,
        goal_evidence=_parse_goal_evidence(data.get("goal_evidence")),
    )


def _parse_goal_evidence(raw: Any) -> dict[str, str]:
    """解析模型对「目标已达成」的可核验声明（V2.2 §四）。

    只接受白名单键——模型多写的字段不参与核验，避免它用一堆没人看的键
    把「有证据」这件事伪装出来。
    """
    if not isinstance(raw, dict):
        return {}
    allowed = {"package", "activity", "text", "resource_id"}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if key in allowed and value not in (None, "")
    }


def _parse_decision(data: dict[str, Any]) -> Decision:
    """把 VLM 的 JSON 解析成 Decision（动作 + 当前步骤是否达成）。"""
    action = _parse_action(data)
    return Decision(
        action=action,
        step_done=bool(data.get("step_done")),
        thought=str(data.get("thought") or action.reason or ""),
    )


def verify_transition(
    pre_screenshot_path: str | Path,
    post_screenshot_path: str | Path,
    instruction: str,
    action: dict[str, Any],
) -> VerifyResult:
    """比较执行前后截图，返回严格的 `VerifyResult`。

    解析不出合法取值时抛 `VlmVerifyError`——**绝不**默认当成功。
    空对象、乱码、未知枚举都会走这条路，由 verifier 按「证据不足」处置。
    """
    pre_b64 = _encode_image(pre_screenshot_path)
    post_b64 = _encode_image(post_screenshot_path)
    prompt = (
        "你正在评估一次 Android 模拟器操作是否有效。\n"
        f"任务：{instruction}\n"
        f"执行动作：{action}\n\n"
        "请比较两张截图，判断操作是否让页面进入预期状态。"
        "返回 JSON：{\"result\": \"ok|error|done\", \"reason\": \"简短原因\"}"
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{pre_b64}", "detail": _image_detail("verify")},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{post_b64}", "detail": _image_detail("verify")},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
    except (VlmError, json.JSONDecodeError, KeyError) as exc:
        raise VlmVerifyError(f"无法解析 VLM 验证结果: {exc}") from exc

    raw = data.get("result")
    if raw is None:
        raise VlmVerifyError(f"VLM 未返回 result 字段（原文：{data!r}）")
    try:
        return VerifyResult(str(raw).strip().lower())
    except ValueError as exc:
        raise VlmVerifyError(f"VLM 返回未知验证结论：{raw!r}") from exc


RELATION_PROMPT = """你在判断用户新说的话与\"正在执行的任务\"是什么关系。

正在执行的任务：{current}
用户新说：{new}

关系只能从下面五个里选一个：
- subtask：新指令是当前任务的一部分或前置步骤（例如「先帮我查一下酒店」）
- super_task：新指令改变了当前任务的目标，需要重做
- unrelated：与当前任务无关，是另一件事
- duplicate：与当前任务重复，没必要再做一遍
- interrupt：需要立刻打断当前任务，优先做这件

仅返回 JSON，不要解释：
{{"relation": "subtask", "confidence": 0.0, "reason": "简短理由"}}
"""


def classify_relation(instruction: str, current_instruction: str) -> TaskRelationResult:
    """让 LLM 判断新指令与当前任务的关系（Classifier 的第三层信号）。

    纯文本调用、不需要截图，成本远低于决策与验证，可以放心在每次注入时调用。
    """
    from models.task_relation import TaskRelation, TaskRelationResult

    prompt = RELATION_PROMPT.format(current=current_instruction or "（当前没有正在执行的任务）", new=instruction)
    try:
        content = _call_vlm([{"role": "user", "content": prompt}])["content"]
        data = _extract_json(content)
    except (VlmError, json.JSONDecodeError) as exc:
        raise VlmError(f"关系判定失败: {exc}") from exc

    raw_relation = str(data.get("relation", "")).strip().lower()
    try:
        relation = TaskRelation(raw_relation)
    except ValueError:
        return TaskRelationResult(reason=f"LLM 返回了未知关系：{raw_relation!r}")

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.5

    return TaskRelationResult(
        relation=relation,
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(data.get("reason") or ""),
    )
