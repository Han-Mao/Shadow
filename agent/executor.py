"""Action JSON → DeviceController 调用。"""
from __future__ import annotations

from device.adb import AdbController, AdbError
from models.action import Action, ActionType, Point
from vision import grounding
from vision.grounding import GroundingError


def _split_launch(value: str) -> tuple[str, str | None]:
    if "/" in value:
        package, activity = value.split("/", 1)
        return package, activity or None
    return value, None


def _parse_swipe(target: Point | str | None) -> tuple[int, int, int, int]:
    if isinstance(target, Point):
        return target.x, target.y, target.x, target.y
    if isinstance(target, str):
        parts = [int(float(p.strip())) for p in target.split(",")]
        if len(parts) == 4:
            return tuple(parts)
    raise AdbError("SWIPE 需要起终点坐标，格式：x1,y1,x2,y2")


def execute(adb: AdbController, action: Action, ui_tree: str | None = None) -> dict:
    """执行 Action 并返回结果字典。"""
    try:
        match action.type:
            case ActionType.TAP:
                x, y = grounding.resolve_target(adb, action.target, ui_tree)
                adb.tap(x, y)
                return {"ok": True, "x": x, "y": y}

            case ActionType.LONG_PRESS:
                x, y = grounding.resolve_target(adb, action.target, ui_tree)
                duration = int(action.value) if action.value else 800
                adb.long_press(x, y, duration)
                return {"ok": True, "x": x, "y": y, "duration": duration}

            case ActionType.SWIPE:
                x1, y1, x2, y2 = _parse_swipe(action.target)
                duration = int(action.value) if action.value else 300
                adb.swipe(x1, y1, x2, y2, duration)
                return {"ok": True, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration}

            case ActionType.TYPE:
                if not action.value:
                    raise AdbError("TYPE 操作需要提供 value")
                adb.type_text(action.value)
                return {"ok": True, "text": action.value}

            case ActionType.BACK:
                adb.back()
                return {"ok": True}

            case ActionType.HOME:
                adb.home()
                return {"ok": True}

            case ActionType.LAUNCH:
                if not action.value:
                    raise AdbError("LAUNCH 操作需要提供 value（package/activity）")
                package, activity = _split_launch(action.value)
                adb.launch(package, activity)
                return {"ok": True, "package": package, "activity": activity}

            case ActionType.WAIT:
                duration = int(action.value) if action.value else 1000
                adb.wait(duration)
                return {"ok": True, "duration": duration}

            case ActionType.DONE:
                return {"ok": True, "done": True}

            case _:
                raise AdbError(f"未知 Action 类型: {action.type}")
    except (AdbError, GroundingError) as exc:
        return {"ok": False, "error": str(exc)}
