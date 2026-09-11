"""Action Schema（§3.2）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ActionType(str, Enum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    TYPE = "type"
    SWIPE = "swipe"
    BACK = "back"
    HOME = "home"
    LAUNCH = "launch"
    WAIT = "wait"
    DONE = "done"


class Point(BaseModel):
    x: int
    y: int


class Action(BaseModel):
    type: ActionType
    target: Point | str | None = None
    value: str | None = None
    reason: str = ""
