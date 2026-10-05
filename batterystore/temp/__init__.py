"""温控：降额曲线、额定值基线与冷却。"""

from __future__ import annotations

from .baseline import BaselineStamp, ThermalProfile, TempBaseline, profile_for_rating
from .control import Cooler
from .curve import DerateCurve
from .snapshot import TempSnapshot
from .temp import TempControl

__all__ = [
    "BaselineStamp",
    "Cooler",
    "DerateCurve",
    "TempBaseline",
    "TempControl",
    "TempSnapshot",
    "ThermalProfile",
    "profile_for_rating",
]
