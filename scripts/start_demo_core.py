"""启动 Shadow Core（真机演示用）—— 自动加载演示需要的环境变量。

演示要同时挂两组变量：`artifacts/vlm_env.sh`（VLM）与 `artifacts/phone_env.sh`
（设备端点）。它们都在 .gitignore 内（含密钥与令牌），所以 Core 必须由这个脚本启动
—— 直接 `uvicorn api.server:app` 会因为缺 `VLM_API_KEY` / `SHADOW_ANDROID_BRIDGE_URL`
而失败，或者更糟：跑到第一次决策才发现。

    python scripts/start_demo_core.py
    python scripts/start_demo_core.py --port 8000
    python scripts/start_demo_core.py --print-only      # 只检查变量，不启动

**为什么不直接 `source` 那两个 .sh**：Windows 上写出来的文件可能是 CRLF，
`source` 会把 `\r` 一起塞进变量值里——表现是「令牌明明抄对了却一直 401」这类
极难定位的错。这里逐行解析并 strip，把那类问题堵在启动期。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

ENV_FILES = ("artifacts/vlm_env.sh", "artifacts/phone_env.sh")
REQUIRED = (
    "VLM_BASE_URL",
    "VLM_API_KEY",
    "VLM_MODEL",
    "SHADOW_DEVICE_BACKEND",
    "SHADOW_ANDROID_BRIDGE_URL",
    "SHADOW_ANDROID_BRIDGE_TOKEN",
)
SECRET_HINTS = ("KEY", "TOKEN")


def load_env_files(paths: tuple[str, ...]) -> dict[str, str]:
    """逐行解析 `export KEY=VALUE`，不做 shell 求值（也不需要 source）。"""
    loaded: dict[str, str] = {}
    for raw in paths:
        path = pathlib.Path(raw)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("export ") or "=" not in line:
                continue
            key, _, value = line[len("export "):].partition("=")
            loaded[key.strip()] = value.strip().strip('"').strip()
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 Shadow Core（真机演示）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--print-only", action="store_true", help="只检查变量并退出，不启动服务")
    args = parser.parse_args()

    # 脚本可能从任何目录被调用，先切到项目根。这一步做两件事：
    #  ① 让 `artifacts/...` 这类相对路径稳定；
    #  ② 让 `uvicorn.run("api.server:app")` 能在 sys.path 里找到 `api` 包——
    #     字符串导入是按 sys.path 找模块的，而脚本直接运行时 sys.path[0] 是
    #     `scripts/` 而不是项目根，少了这一步会报 `ModuleNotFoundError: No module named 'api'`。
    root = pathlib.Path(__file__).resolve().parent.parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.environ.update(load_env_files(ENV_FILES))

    print("=" * 68)
    print("Shadow Core · 真机演示环境")
    print("=" * 68)
    missing: list[str] = []
    for key in REQUIRED:
        value = os.environ.get(key, "")
        if not value:
            missing.append(key)
            print(f"  [缺失] {key}")
        else:
            shown = "（已设置）" if any(h in key for h in SECRET_HINTS) else value
            print(f"  [ OK ] {key} = {shown}")

    if missing:
        print()
        print(f"上面 {len(missing)} 个变量没加载到。检查这两个文件是否存在、且格式为 `export KEY=VALUE`：")
        for name in ENV_FILES:
            exists = "存在" if pathlib.Path(name).exists() else "**不存在**"
            print(f"  {name}  ({exists})")
        return 2

    if args.print_only:
        print("\n变量齐了（--print-only，未启动服务）。")
        return 0

    print()
    print(f"启动：http://{args.host}:{args.port}")
    print(f"接口文档：http://{args.host}:{args.port}/docs")
    print()
    print("想在**手机上**发任务的话（另开一个终端保持这条连接）：")
    print("  adb -s <手机序列号> reverse tcp:8000 tcp:8000")
    print("  然后手机浏览器打开 http://127.0.0.1:8000/docs")
    print("  → 展开 POST /tasks → Try it out → 填 instruction → Execute")
    print("  （走 adb 通道，绕开 Windows 防火墙；adb 一断 reverse 就没了）")
    print()
    print("Stop: Ctrl+C")
    print("=" * 68)

    import uvicorn

    uvicorn.run("api.server:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
