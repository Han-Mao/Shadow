"""Action → DeviceController 调用（V3.3 §1：只依赖设备**端口**，不依赖某个后端）。

本模块刻意**不 import 任何后端模块**：它 `from device.controller import DeviceController`
拿到协议，`build_default_input(device)` 问设备自己要用哪条输入通道。
这样「PC 用 ADB 控制手机」和「手机自己跑」共用同一份执行逻辑
（方案文档 §1 的「Executor → DeviceController → AndroidAdapter」那条链）。
"""
from __future__ import annotations

from device.controller import DeviceController, DeviceError
from device.input import build_default_input
from models.action import DURATION_RANGE_MS, Action, ActionType, Point
from models.exceptions import ActionArgumentError
from models.retry import classify_exception
from vision import grounding
from vision.grounding import GroundingError


LAUNCH_SETTLE_MS = 6_000
"""启动应用后等它稳定下来的时长。

应用冷启动要几秒，而观察紧跟在动作之后 —— 不等就必然看到启动页或桌面，
验证于是判「页面未进入预期状态」。**动作其实是成功的**，但这条误判会连锁成
「判失败 → 重做 → 再判失败」，是 2026-09-18 真机上观察到的第一种死循环。

6s 是**量出来的**（2026-09-18 在 vivo V2352A 上逐秒取样）：

    +0s   launch 发出
    +2s   前台 = com.vivo.appfilter/.activity.AppJumpPromptActivity    ← vivo「应用跳转」确认框
    +4s   前台 = 同上
    +6s   前台 = com.xunmeng.pinduoduo/.ui.activity.MainFrameActivity  ← 应用到位

国产 ROM 会拦「一个应用启动另一个应用」，中间插一个跳转确认框，
**约 5-6 秒后自动放行**（不需要用户点，但期间前台既不是原应用、也不是目标应用）。
原先取 2.5s 正好落在那段弹窗里，于是每次 launch 都判失败——
**这不是代码问题，是把厂商弹窗的耗时算短了**。
"""


def _split_launch(value: str) -> tuple[str, str | None]:
    if "/" in value:
        package, activity = value.split("/", 1)
        return package, activity or None
    return value, None


def _parse_duration(value: str | None, default: int) -> int:
    """时长参数统一解析。VLM 常返回 "800" / "800ms" / "1秒"，非法值必须转成明确的错误。"""
    if value is None or str(value).strip() == "":
        return default
    try:
        duration = int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ActionArgumentError(f"时长需为毫秒数，收到 {value!r}") from exc

    low, high = DURATION_RANGE_MS
    if not low <= duration <= high:
        raise ActionArgumentError(f"时长 {duration}ms 超出允许范围 {low}-{high}ms")
    return duration


def _parse_swipe(target: Point | str | None) -> tuple[int, int, int, int]:
    """解析滑动起终点。只接受 Point（退化为点按）或 "x1,y1,x2,y2" 字符串。"""
    if isinstance(target, Point):
        x, y = round(target.x), round(target.y)
        return x, y, x, y

    if isinstance(target, str):
        parts = [p.strip() for p in target.replace("，", ",").split(",")]
        if len(parts) != 4:
            raise ActionArgumentError(f"SWIPE 需要 4 个坐标（x1,y1,x2,y2），收到 {target!r}")
        try:
            x1, y1, x2, y2 = (int(float(p)) for p in parts)
        except (TypeError, ValueError) as exc:
            raise ActionArgumentError(f"SWIPE 坐标无法解析为数字: {target!r}") from exc
        return x1, y1, x2, y2

    raise ActionArgumentError("SWIPE 需要起终点坐标，格式：x1,y1,x2,y2")


def execute(device: DeviceController, action: Action, ui_tree: str | None = None) -> dict:
    """执行 Action 并返回结果字典。

    约定：本函数**永不抛异常**。任何失败都收敛成 {"ok": False, "error": ...}，
    否则异常会穿透主循环，让整个任务以 HTTP 500 中断。
    """
    try:
        match action.type:
            case ActionType.TAP:
                x, y = grounding.resolve_target(device, action.target, ui_tree)
                device.tap(x, y)
                return {"ok": True, "x": x, "y": y}

            case ActionType.LONG_PRESS:
                x, y = grounding.resolve_target(device, action.target, ui_tree)
                duration = _parse_duration(action.value, 800)
                device.long_press(x, y, duration)
                return {"ok": True, "x": x, "y": y, "duration": duration}

            case ActionType.SWIPE:
                x1, y1, x2, y2 = _parse_swipe(action.target)
                duration = _parse_duration(action.value, 300)
                device.swipe(x1, y1, x2, y2, duration)
                return {"ok": True, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration}

            case ActionType.TYPE:
                if not action.value:
                    raise ActionArgumentError("TYPE 操作需要提供 value")
                # V2.9 P1：中文输入链路必须与 `/text` API 一致。
                # 原来直接 `adb.type_text()` 只支持安全 ASCII，Agent 生成的
                # `Action(TYPE, "给妈妈发消息")` 会在设备端被吞成空 —— 同一个人工
                # `/text` 能输中文、Agent 反而输不了，是功能割裂。
                # 统一走 `build_default_input`，由**控制器自己**声明该用哪条通道（V3.3 §5）：
                # ADB 后端是「安全 ASCII 走 `input text`，其余走 ADB Keyboard 广播」
                # （与 api/server.py 的 /text 端点同一条链路），
                # Android 后端是 Accessibility `ACTION_SET_TEXT`（中文英文同一条路）。
                provider = build_default_input(device)
                provider.input(action.value)
                return {"ok": True, "text": action.value, "provider": provider.name}

            case ActionType.BACK:
                device.back()
                return {"ok": True}

            case ActionType.HOME:
                device.home()
                return {"ok": True}

            case ActionType.LAUNCH:
                # 模型经常把应用名/包名塞进 `target`、把 `value` 留空 ——
                # 2026-09-18 真机上三次尝试有两次是这么错的（`target="拼多多" value=""`），
                # 两次重试就这么白费了，而 AppResolver 根本没机会被调用。
                #
                # `target` 在 launch 语义下**没有别的用途**，所以这里宽容一点：
                # value 为空、而 target 是非空字符串时，就当包名/应用名用。
                # 读宽写严 —— 这个宽容不会把一种动作变成另一种动作。
                raw = action.value or (
                    action.target
                    if isinstance(action.target, str) and action.target.strip()
                    else ""
                )
                if not raw:
                    raise ActionArgumentError("LAUNCH 操作需要提供应用名或包名（写在 value 里）")
                package, activity = _split_launch(str(raw))
                # 只给包名时走端口上的 `launch_app`（方案文档 §6「App 启动改成 Android Intent」）：
                # ADB 侧是 `monkey -p`，Android 侧是 PackageManager + startActivity。
                # 这里刻意不写「if 后端是 android」——那等于把后端类型判断塞回核心。
                if activity:
                    device.launch(package, activity)
                else:
                    device.launch_app(package)
                # 见 LAUNCH_SETTLE_MS 的说明：不等的话，紧跟其后的观察只会看到
                # 启动页，验证判「未进入预期状态」，而动作其实已经成功。
                device.wait(LAUNCH_SETTLE_MS)
                return {"ok": True, "package": package, "activity": activity}

            case ActionType.WAIT:
                duration = _parse_duration(action.value, 1000)
                device.wait(duration)
                return {"ok": True, "duration": duration}

            case ActionType.DONE | ActionType.DONE_REQUEST:
                # 「申请完成」不是设备动作，没有命令要发。真正的完成判定在
                # `agent.goal_verifier`——模型说 done 只是一次申请（V2.2 §四）。
                return {"ok": True, "done": True}

            case _:
                raise ActionArgumentError(f"未知 Action 类型: {action.type}")
    except (DeviceError, ActionArgumentError, GroundingError) as exc:
        # V2.7 P2-2：失败除了给文本，还要带上结构化错误类别。
        # 下游（verifier → retry 策略）宁可读这个字段，也不要从一句中文里猜它属于哪类。
        return {"ok": False, "error": str(exc), "error_class": classify_exception(exc).value}
    except Exception as exc:  # noqa: BLE001 - 兜底，保证主循环拿到的永远是结构化结果
        return {
            "ok": False,
            "error": f"执行异常 {type(exc).__name__}: {exc}",
            "error_class": classify_exception(exc).value,
        }
