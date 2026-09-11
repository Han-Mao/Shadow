"""VLM 调用：页面理解、计划生成、单步决策、执行验证。不输出 ADB 命令。"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from models.action import Action, ActionType, Point

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o"


class VlmError(RuntimeError):
    pass


class VlmParseError(VlmError):
    pass


def _encode_image(path: str | Path) -> str:
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("utf-8")


def _extract_json(text: str) -> dict[str, Any]:
    """从可能包含 markdown 代码块的文本中提取 JSON。"""
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    return json.loads(text)


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
    except Exception as exc:
        return f"UI 树解析失败: {exc}"


def build_plan_prompt(instruction: str, screenshot_path: str, ui_tree: str | None) -> str:
    tree = _compact_ui_tree(ui_tree)
    return (
        "你正在控制一部 Android 模拟器。请根据当前页面截图与可点击元素列表，"
        f"为以下任务制定一个简洁的语义级执行计划（3-8 步）：\n{instruction}\n\n"
        "可点击元素（部分）：\n" + (tree or "无") + "\n\n"
        "请仅返回 JSON：\n"
        "{\n"
        '  "plan": ["步骤1", "步骤2", ...]\n'
        "}"
    )


def build_decision_prompt(
    instruction: str,
    screenshot_path: str,
    ui_tree: str | None,
    history: list[dict[str, Any]],
    plan: list[str],
) -> str:
    steps = "\n".join(
        f"步骤 {o['step']}: {o.get('action')} -> {o.get('status')} ({o.get('message')})"
        for o in history[-5:]
    )
    plan_text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(plan)) if plan else "无"
    tree = _compact_ui_tree(ui_tree)
    return (
        "你正在控制一部 Android 模拟器。任务：\n" + instruction + "\n\n"
        "整体计划：\n" + plan_text + "\n\n"
        "最近执行记录：\n" + (steps or "无") + "\n\n"
        "当前页面可点击元素（部分）：\n" + (tree or "无") + "\n\n"
        "请以 JSON 格式返回下一步操作，不要包含其他解释：\n"
        "{\n"
        '  "thought": "对当前页面的简短分析",\n'
        '  "action_type": "tap|long_press|type|swipe|back|home|launch|wait|done",\n'
        '  "target": "点击目标的描述或坐标，如 {\"x\":360,\"y\":600} 或 \"搜索按钮\"",\n'
        '  "value": "当 action_type=type 时填写要输入的文本；swipe 时填写 x1,y1,x2,y2；wait 时填写毫秒",\n'
        '  "done": false\n'
        "}\n"
        "坐标优先返回屏幕绝对像素；若不确定，返回可点击元素的文本或 content-desc 描述。"
    )


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

    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
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
    except (KeyError, IndexError, TypeError) as exc:
        raise VlmError(f"无法解析 VLM 响应结构: {exc}") from exc


def generate_plan(instruction: str, screenshot_path: str, ui_tree: str | None) -> list[str]:
    """根据首屏生成语义级步骤计划。"""
    image_b64 = _encode_image(screenshot_path)
    prompt = build_plan_prompt(instruction, screenshot_path, ui_tree)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "high"},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
        plan = data.get("plan", [])
        if isinstance(plan, list):
            return [str(p) for p in plan]
    except (VlmError, json.JSONDecodeError, KeyError):
        pass
    return []


def decide_next_action(
    instruction: str,
    screenshot_path: str,
    ui_tree: str | None,
    history: list[dict[str, Any]] | None = None,
    plan: list[str] | None = None,
) -> Action:
    """根据截图与任务历史，调用 VLM 返回下一步 Action。"""
    image_b64 = _encode_image(screenshot_path)
    prompt = build_decision_prompt(instruction, screenshot_path, ui_tree, history or [], plan or [])
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "high"},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
    except json.JSONDecodeError as exc:
        raise VlmParseError(f"无法解析 VLM 响应: {exc}") from exc

    return _parse_action(data)


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
        target = Point(x=int(target.get("x", 0)), y=int(target.get("y", 0)))

    value = data.get("value")
    reason = data.get("thought", "")

    if data.get("done") and action_type != ActionType.DONE:
        action_type = ActionType.DONE

    return Action(type=action_type, target=target, value=value, reason=reason)


def verify_transition(
    pre_screenshot_path: str | Path,
    post_screenshot_path: str | Path,
    instruction: str,
    action: dict[str, Any],
) -> str:
    """比较执行前后截图，返回 'ok' | 'error' | 'done'。"""
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
                    "image_url": {"url": f"data:image/png;base64,{pre_b64}", "detail": "high"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{post_b64}", "detail": "high"},
                },
            ],
        }
    ]

    try:
        content = _call_vlm(messages)["content"]
        data = _extract_json(content)
    except (json.JSONDecodeError, KeyError) as exc:
        raise VlmError(f"无法解析 VLM 验证结果: {exc}") from exc

    return str(data.get("result", "ok")).lower()
