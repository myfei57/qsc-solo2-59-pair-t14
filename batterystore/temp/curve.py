"""温度降额曲线。

曲线分三段：起点以下不降额，起点到满额点之间线性下降，满额点以上压到下限
系数。系数直接乘以 PCS 功率指令，控制台展示的是同一份系数。
"""

from __future__ import annotations

from ..config import TempLimits


class DerateCurve:
    """两段折线的降额曲线。"""

    def __init__(self, limits: TempLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> TempLimits:
        return self._limits

    def ratio(self, temperature_c: float, profile=None) -> float:
        """返回给定温度下的降额系数。"""

        start_c = self._limits.derate_start_c if profile is None else profile.derate_start_c
        full_c = self._limits.derate_full_c if profile is None else profile.derate_full_c
        floor = self._limits.derate_floor_ratio if profile is None else profile.derate_floor_ratio
        value = float(temperature_c)
        if value <= start_c:
            return 1.0
        if value >= full_c:
            return floor
        span = full_c - start_c
        weight = (value - start_c) / span
        return 1.0 - (1.0 - floor) * weight

    def band(self, temperature_c: float, profile=None) -> str:
        """返回温度所处的档位名。"""

        start_c = self._limits.derate_start_c if profile is None else profile.derate_start_c
        full_c = self._limits.derate_full_c if profile is None else profile.derate_full_c
        warn_c = self._limits.warn_c if profile is None else min(self._limits.warn_c, start_c)
        value = float(temperature_c)
        if value >= full_c:
            return "full_derate"
        if value >= start_c:
            return "derated"
        if value >= warn_c:
            return "warm"
        return "normal"

    def points(self, profile=None) -> list[dict]:
        """返回曲线控制点，供控制台画图。"""

        start_c = self._limits.derate_start_c if profile is None else profile.derate_start_c
        full_c = self._limits.derate_full_c if profile is None else profile.derate_full_c
        floor = self._limits.derate_floor_ratio if profile is None else profile.derate_floor_ratio
        return [
            {"temperature_c": start_c, "ratio": 1.0},
            {"temperature_c": full_c, "ratio": floor},
        ]
