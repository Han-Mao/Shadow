"""真机演示前的环境预检。

演示链条很长（adb → 装包 → 辅助功能 → 投屏 → 端点 → Core → VLM），
任何一环断了，现场表现都是「任务没动静」，很难判断卡在哪。
这个脚本把能自动查的都查一遍，只把必须人眼看的留给你。

用法：
    python scripts/phone_demo_preflight.py
    python scripts/phone_demo_preflight.py --bridge-url http://192.168.1.20:8765 --token <手机上的令牌>

它只读不写：不装包、不改设置、不发任务。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

PKG = "com.bluewhale.shadow"
SERVICE = f"{PKG}/{PKG}.device.ShadowAccessibilityService"
APK = pathlib.Path("android/app/build/outputs/apk/debug/app-debug.apk")

OK, MISS, WARN, INFO = "[ OK ]", "[MISS]", "[WARN]", "[ .. ]"


def find_adb() -> str | None:
    """先在 PATH 里找，再找工具链目录（本仓库的默认安装位置）。"""
    found = shutil.which("adb")
    if found:
        return found
    for base in (
        pathlib.Path(os.path.expanduser("~")) / ".workbuddy" / "binaries",
        pathlib.Path("C:/Android/Sdk"),
    ):
        for candidate in base.glob("**/platform-tools/adb.exe"):
            return str(candidate)
    return None


def run(adb: str, *args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [adb, *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "<命令超时>"
    except OSError as exc:
        return 1, f"<无法执行 adb: {exc}>"


def probe_http(url: str, token: str | None) -> tuple[bool, str]:
    req = urllib.request.Request(url.rstrip("/") + "/health")
    if token:
        req.add_header("X-Shadow-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"HTTP {resp.status} {resp.read(400).decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as exc:
        body = exc.read(200).decode("utf-8", "replace")
        hint = "（401 = 令牌不对）" if exc.code == 401 else ""
        return False, f"HTTP {exc.code} {body} {hint}"
    except Exception as exc:  # noqa: BLE001 — 预检脚本，任何异常都要变成一行提示
        return False, f"连不上：{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="真机演示环境预检")
    parser.add_argument("--bridge-url", help="手机页面上显示的地址，例如 http://192.168.1.20:8765")
    parser.add_argument("--token", help="手机页面上显示的令牌")
    args = parser.parse_args()

    blocked: list[str] = []
    pending: list[str] = []

    print("=" * 68)
    print("Shadow 真机演示 · 环境预检")
    print("=" * 68)

    print("\n[1] adb")
    adb = find_adb()
    if not adb:
        print(f"{MISS} 找不到 adb")
        print("      下载 https://dl.google.com/android/repository/platform-tools-latest-windows.zip")
        print("      解压到 ~/.workbuddy/binaries/platform-tools/（本仓库脚本会自动在这里找）")
        blocked.append("adb 未安装")
        print("\n没有 adb 也可以走「手机手动装 APK」这条路——见文档 §B。")
        adb = None
    else:
        _, ver = run(adb, "version")
        first = ver.strip().splitlines()[0] if ver.strip() else "?"
        print(f"{OK} {adb}")
        print(f"      {first}")

    device_serial = None
    if adb:
        print("\n[2] 设备连接")
        _, out = run(adb, "devices", "-l")
        # adb 首次调用会把 "* daemon not running..." 混进 stdout，
        # 那两行也是「两个 token」，不过滤就会被当成设备。
        lines = [
            ln for ln in out.splitlines()[1:]
            if ln.strip() and not ln.lstrip().startswith("*")
        ]
        if not lines:
            print(f"{MISS} 没有设备")
            pending.append("用 USB 线连上手机，并在手机上开启「USB 调试」")
            print("      手机侧：设置 → 关于手机 → 连点 7 次「版本号」→ 返回 → 开发者选项 → USB 调试")
            print("      （数据线连接后手机会弹「允许 USB 调试吗」，选允许）")
        else:
            for ln in lines:
                parts = ln.split()
                serial, state = parts[0], parts[1] if len(parts) > 1 else "?"
                model = ""
                m = re.search(r"model:(\S+)", ln)
                if m:
                    model = m.group(1).replace("_", " ")
                if state == "device":
                    print(f"{OK} {serial}  {model}")
                    device_serial = serial
                elif state == "unauthorized":
                    print(f"{MISS} {serial}  {model} — 未授权")
                    pending.append("手机屏幕上弹出的「允许 USB 调试」要点「允许」")
                else:
                    print(f"{WARN} {serial}  state={state}")
                    pending.append(f"设备 {serial} 状态异常（{state}），试试 adb kill-server 后重插")

    if device_serial:
        print("\n[3] 手机本体")
        code, out = run(adb, "-s", device_serial, "shell", "getprop", "ro.build.version.release")
        release = out.strip() if code == 0 else "?"
        code, out = run(adb, "-s", device_serial, "shell", "getprop", "ro.product.model")
        model = out.strip() if code == 0 else "?"
        print(f"{INFO} {model} / Android {release}")
        try:
            if int(release.split(".")[0]) < 8:
                print(f"{WARN} minSdk 是 26（Android 8.0），这台机器可能装不上")
        except ValueError:
            pass

        print("\n[4] 包是否已安装")
        code, out = run(adb, "-s", device_serial, "shell", "pm", "list", "packages", PKG)
        if f"package:{PKG}" in out:
            code, ver = run(adb, "-s", device_serial, "shell", "dumpsys", "package", PKG)
            vm = re.search(r"versionName=(\S+)", ver)
            print(f"{OK} 已安装（versionName={vm.group(1) if vm else '?'}）")
        else:
            print(f"{MISS} 没装")
            if APK.exists():
                print(f"      装法：adb -s {device_serial} install -r {APK.as_posix()}")
            pending.append("安装 APK")

        print("\n[5] 辅助功能服务")
        code, out = run(adb, "-s", device_serial, "shell", "settings", "get", "secure",
                        "enabled_accessibility_services")
        if SERVICE in out or PKG in out:
            print(f"{OK} 已开启（{out.strip()[:80]}）")
        else:
            print(f"{MISS} 未开启")
            print("      手机侧：设置 → 无障碍/辅助功能 → Shadow 设备端点 → 开启")
            pending.append("开启辅助功能服务（不开就没有 UI 树和手势）")

        print("\n[6] 屏幕捕获（只能人眼看）")
        print(f"{INFO} 应用内点「授权屏幕捕获」；页面上「屏幕捕获」一行显示「已就绪」即可")
        print("      注意：重启应用后此授权会失效（系统行为），要重新授权")
        pending.append("在应用里授权屏幕捕获（不开就没有截图，VLM 无法决策）")

        print("\n[7] 设备端点是否在跑")
        code, out = run(adb, "-s", device_serial, "shell", "dumpsys", "activity", "services", PKG)
        if ".endpoint.DeviceEndpointService" in out:
            print(f"{OK} DeviceEndpointService 在运行（通知栏应有常驻通知）")
        else:
            print(f"{MISS} 没在运行")
            print("      手机侧：应用里点「启动设备端点」")
            pending.append("启动设备端点")
        code, out = run(adb, "-s", device_serial, "shell", "ip", "route")
        lan = ""
        m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            lan = m.group(1)
            print(f"{INFO} 手机局域网 IP：{lan}")
            print(f"      若端点已启动，Core 侧应填 http://{lan}:8765")

    print("\n[8] 端点连通性（Core 侧视角）")
    if args.bridge_url:
        ok, detail = probe_http(args.bridge_url, args.token)
        print(f"{OK if ok else MISS} {args.bridge_url}/health → {detail}")
        if ok and '"state"' in detail and '"device"' not in detail:
            print(f"{WARN} state 不是 device：权限还没齐（返回体里会点名缺哪项）")
        if not ok:
            pending.append("端点连不通：确认手机与 PC 在同一个局域网、端点已启动、令牌正确")
    else:
        print(f"{INFO} 未提供 --bridge-url，跳过")
        print("      用法：python scripts/phone_demo_preflight.py --bridge-url http://<手机IP>:8765 --token <令牌>")

    print("\n[9] VLM 配置（真机跑任务必须有）")
    vlm = {k: os.environ.get(k) for k in ("VLM_BASE_URL", "VLM_API_KEY", "VLM_MODEL")}
    missing = [k for k, v in vlm.items() if not v]
    for k, v in vlm.items():
        shown = "（已设置）" if k == "VLM_API_KEY" and v else (v or "（未设置）")
        print(f"{OK if v else MISS} {k} = {shown}")
    if missing:
        print("      VLM 走 HTTP，不跑在手机上（方案文档 §8）。没有它，planner 无法决策。")
        pending.append("配置 VLM_BASE_URL / VLM_API_KEY / VLM_MODEL")
    else:
        print(f"{INFO} 自检：curl -s -X POST {vlm['VLM_BASE_URL']}/chat/completions "
              f"-H 'Authorization: Bearer $VLM_API_KEY' -H 'Content-Type: application/json' "
              f"-d '{{\"model\":\"{vlm['VLM_MODEL']}\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'")

    print("\n[10] 设备后端选择")
    backend = os.environ.get("SHADOW_DEVICE_BACKEND", "(未设置)")
    if backend == "android":
        print(f"{OK} SHADOW_DEVICE_BACKEND=android")
        url = os.environ.get("SHADOW_ANDROID_BRIDGE_URL", "(未设置)")
        print(f"{OK if url != '(未设置)' else MISS} SHADOW_ANDROID_BRIDGE_URL = {url}")
        if url == "(未设置)":
            pending.append("export SHADOW_ANDROID_BRIDGE_URL=<手机页面上的地址>")
    elif backend == "adb":
        print(f"{OK} SHADOW_DEVICE_BACKEND=adb（PC 用 adb 控制手机，注意别和端点模式混用）")
    else:
        print(f"{WARN} {backend} —— 真机演示应设为 android")
        pending.append("export SHADOW_DEVICE_BACKEND=android")

    print("\n" + "=" * 68)
    if pending:
        print(f"还差 {len(pending)} 件事：")
        for i, item in enumerate(pending, 1):
            print(f"  {i}. {item}")
    else:
        print("全部就绪，可以跑任务了：")
        print('  curl -X POST http://127.0.0.1:8000/tasks '
              '-H "Content-Type: application/json" '
              "-d '{\"instruction\": \"打开设置，查看电池电量\"}'")
    if blocked:
        print("\n被硬阻塞的项：")
        for item in blocked:
            print(f"  - {item}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
