"""VLM 端点自检：在把任务交给它之前，先量清它「能不能用、有多快、边界在哪」。

演示时最怕的不是模型笨，而是**它是另一种坏法**：端点通、返回 200，但
① 慢到超过 `vision/vlm.py` 的 `VLM_TIMEOUT_SECONDS`（60s）——任务表现为「一直在想然后失败」；
② 不按 prompt 吐 JSON —— 表现为 `VlmParseError`，任务在建计划阶段就挂；
③ 多图请求直接报错 —— `verify_transition` 一次要传两张截图。
这三条都不会在「端点 reachable」的检查里暴露，所以单测出来。

    python scripts/vlm_selftest.py
    python scripts/vlm_selftest.py --image artifacts/shadow_app_screen.png
    python scripts/vlm_selftest.py --model llava:7b --base-url http://127.0.0.1:11434/v1

只读：不改任何配置、不写任何文件。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import sys
import time

import httpx

# 与 vision/vlm.py 保持一致（那是权威值，这里只是拿来对比）
SHADOW_VLM_TIMEOUT = 60.0

OK, BAD, WARN, INFO = "[ OK ]", "[FAIL]", "[WARN]", "[ .. ]"


def encode_image(path: pathlib.Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def call(base: str, key: str, model: str, messages: list, *, timeout: float, max_tokens: int = 512):
    """返回 (ok, 耗时秒, 正文或错误)。"""
    started = time.monotonic()
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — 自检脚本，任何异常都变成一行结论
        return False, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    if resp.status_code != 200:
        return False, elapsed, f"HTTP {resp.status_code}: {resp.text[:300]}"
    try:
        message = resp.json()["choices"][0]["message"]
        content = message.get("content")
        if isinstance(content, list):
            content = next((b.get("text", "") for b in content if b.get("type") == "text"), "")
        return True, elapsed, content if isinstance(content, str) else str(content)
    except Exception as exc:  # noqa: BLE001
        return False, elapsed, f"响应结构不符合 OpenAI 约定：{type(exc).__name__}: {exc}"


def find_default_image() -> pathlib.Path | None:
    for candidate in ("artifacts/shadow_app_screen.png", "artifacts/phone_demo_screen.png"):
        path = pathlib.Path(candidate)
        if path.exists():
            return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="VLM 端点自检")
    parser.add_argument("--base-url", default=os.getenv("VLM_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("VLM_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("VLM_MODEL", ""))
    parser.add_argument("--image", help="用于图文测试的截图，默认自动找 artifacts/ 下的")
    parser.add_argument("--timeout", type=float, default=180.0, help="单次请求超时（默认 180s，比 Shadow 的 60s 宽松，以便量出真实耗时）")
    args = parser.parse_args()

    base = (args.base_url or "").rstrip("/")
    problems: list[str] = []

    print("=" * 70)
    print("VLM 端点自检")
    print("=" * 70)

    print("\n[1] 配置")
    for name, value in (("VLM_BASE_URL", args.base_url), ("VLM_API_KEY", args.api_key), ("VLM_MODEL", args.model)):
        shown = "（已设置）" if name == "VLM_API_KEY" and value else (value or "（未设置）")
        print(f"{OK if value else BAD} {name} = {shown}")
        if not value:
            problems.append(f"{name} 未设置")
    if problems:
        print("\n配置不全，后面没法测。vision/vlm.py 要求这三个都非空 —— api_key 只做非空校验，")
        print("所以指向本地 Ollama 时随便填一个非空值即可）。")
        return 2

    print("\n[2] 端点可达性")
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{base}/models", headers={"Authorization": f"Bearer {args.api_key}"})
        if resp.status_code == 200:
            ids = [m.get("id") for m in resp.json().get("data", [])]
            print(f"{OK} {base}/models → {len(ids)} 个模型")
            for mid in ids[:10]:
                mark = " ← 本次要用的" if mid == args.model else ""
                print(f"        {mid}{mark}")
            if args.model not in ids:
                print(f"{WARN} 列表里没有 {args.model}，可能是没 pull 或名字拼错")
                problems.append(f"模型 {args.model} 不在端点列表里")
        else:
            print(f"{WARN} {base}/models → HTTP {resp.status_code}（有些端点不实现这个接口，不一定是错）")
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} 连不上 {base}：{type(exc).__name__}: {exc}")
        problems.append("端点连不上")
        return 2

    print("\n[3] 纯文本 + 中文（Shadow 的 prompt 全是中文，这一步看模型的中文听话程度）")
    ok, elapsed, out = call(
        base, args.api_key, args.model,
        [{"role": "user", "content": "只回复两个字：收到"}],
        timeout=args.timeout, max_tokens=32,
    )
    print(f"{OK if ok else BAD} 耗时 {elapsed:.1f}s（Shadow 超时 {SHADOW_VLM_TIMEOUT:.0f}s）")
    print(f"        返回：{out[:120]!r}")
    if ok and elapsed > SHADOW_VLM_TIMEOUT:
        print(f"{WARN} 单次调用的首次响应就超了 Shadow 的 60s 超时——真跑任务会被判失败")
        problems.append("首次推理慢于 Shadow 的 60s 超时（首次含模型加载，第二次通常会快）")
    if not ok:
        problems.append("纯文本调用失败")

    image = pathlib.Path(args.image) if args.image else find_default_image()
    if image and image.exists():
        b64 = encode_image(image)
        print(f"\n[4] 图文（{image}，{image.stat().st_size // 1024} KB）")

        ok2, elapsed2, out2 = call(
            base, args.api_key, args.model,
            [{"role": "user", "content": [
                {"type": "text", "text": "这是一张安卓手机应用截图。请用一句话说明这个应用是做什么的。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            timeout=args.timeout,
        )
        print(f"{OK if ok2 else BAD} 耗时 {elapsed2:.1f}s")
        print(f"        返回：{out2[:200]!r}")
        if not ok2:
            problems.append("图文调用失败")
        if ok2 and elapsed2 > SHADOW_VLM_TIMEOUT:
            problems.append(f"图文推理 {elapsed2:.0f}s，超过 Shadow 的 60s 超时")

        print("\n[5] JSON 约束输出（Shadow 的硬要求：解析不出 JSON 就是 VlmParseError）")
        ok3, elapsed3, out3 = call(
            base, args.api_key, args.model,
            [{"role": "user", "content": [
                {"type": "text", "text": (
                    "根据截图判断下一步操作。只返回 JSON，不要任何解释：\n"
                    '{"action_type": "tap|type|back|done", "target": "元素描述或坐标", "thought": "简短分析"}'
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            timeout=args.timeout,
        )
        print(f"{OK if ok3 else BAD} 耗时 {elapsed3:.1f}s")
        print(f"        原文：{out3[:250]!r}")
        if ok3:
            start, end = out3.find("{"), out3.rfind("}")
            parsed = None
            if start != -1 and end > start:
                try:
                    parsed = json.loads(out3[start:end + 1])
                except json.JSONDecodeError:
                    parsed = None
            if parsed is None:
                print(f"{BAD} 抠不出合法 JSON —— Shadow 的 _extract_json 会抛 VlmParseError")
                problems.append("不吐 JSON（Shadow 需要 JSON 才能决策）")
            else:
                keys = set(parsed)
                print(f"{OK} 解析成功，字段：{sorted(keys)}")
                if "action_type" not in keys:
                    print(f"{WARN} 缺 action_type —— vlm._parse_action 会直接抛错")
                    problems.append("JSON 里没有 action_type")

        print("\n[6] 双图请求（verify_transition 一次要传执行前 + 执行后两张截图）")
        ok4, elapsed4, out4 = call(
            base, args.api_key, args.model,
            [{"role": "user", "content": [
                {"type": "text", "text": "这两张截图是同一页面执行动作前后的对比。只返回 JSON：{\"result\": \"ok|error|done\", \"reason\": \"简短原因\"}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            timeout=args.timeout,
        )
        print(f"{OK if ok4 else BAD} 耗时 {elapsed4:.1f}s")
        if ok4:
            print(f"        返回：{out4[:200]!r}")
        else:
            print(f"        错误：{out4[:300]}")
            print("        多数开源 VLM 只接受单图；`verify_transition` 会因此一直失败（按「证据不足」处理）。")
            problems.append("双图请求失败 —— verify_transition 会持续失败")
    else:
        print(f"\n[4-6] {INFO} 没找到测试截图，跳过图文测试")
        print("        用 --image <png> 指定，或先跑一次手机截图（见脚本 phone_demo_preflight.py）")

    print("\n" + "=" * 70)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for i, item in enumerate(problems, 1):
            print(f"  {i}. {item}")
    else:
        print("全部通过，这个端点可以用。")
    print("=" * 70)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
