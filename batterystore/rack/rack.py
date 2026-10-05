"""电池簇。

一簇由若干电池包串联组成，对外提供四件事：压差与温差度量、SOC 读写、
额定值更新（换电芯）与快照。额定值更新会返回一条变更记录，温控基线据此
判定自己是否需要立即刷新——这是"换完电芯还按旧额定值降额"这类问题的
唯一正确解法。
"""

from __future__ import annotations

from ..config import RackLimits
from .cells import Cell
from .energy import rack_energy_kwh, usable_energy_kwh
from .metrics import RackMetrics, compute_metrics
from .pack import Pack
from .snapshot import RackSnapshot


class Rack:
    """一簇电池。"""

    def __init__(self, rack_id: str, packs: list[Pack], zone: str) -> None:
        if not packs:
            raise ValueError("电池簇至少需要一个电池包")
        self._rack_id = rack_id
        self._packs = list(packs)
        self._zone = zone
        self._soc_pct: float = float(packs[0].cells()[0].soc_pct)
        self._rating_v: float = min(pack.rating_v() for pack in packs)
        self._rating_epoch = 0

    @property
    def rack_id(self) -> str:
        return self._rack_id

    @property
    def zone(self) -> str:
        return self._zone

    @property
    def rating_epoch(self) -> int:
        """返回额定值代际，换一次电芯加一。"""

        return self._rating_epoch

    def packs(self) -> list[Pack]:
        """返回簇内电池包。"""

        return list(self._packs)

    def pack(self, pack_index: int) -> Pack:
        """按序号取电池包。"""

        if not 0 <= pack_index < len(self._packs):
            raise IndexError(f"电池包序号越界: {pack_index}")
        return self._packs[pack_index]

    def rating_map(self) -> dict[str, dict]:
        """返回每个包的额定值和额定值代际。"""

        return {
            pack.pack_id: {"rating_v": round(pack.rating_v(), 4), "rating_epoch": pack.rating_epoch}
            for pack in self._packs
        }

    def pack_ratings(self) -> dict[str, tuple[float, int]]:
        """供温控批量读取的包额定值。"""

        return {pack.pack_id: (pack.rating_v(), pack.rating_epoch) for pack in self._packs}

    def cells(self) -> list[Cell]:
        """返回簇内全部单体。"""

        return [cell for pack in self._packs for cell in pack.cells()]

    def voltage_v(self) -> float:
        """返回簇电压。"""

        return sum(pack.voltage_v() for pack in self._packs)

    def soc_pct(self) -> float:
        """返回当前 SOC。"""

        return self._soc_pct

    def set_soc(self, soc_pct: float) -> float:
        """写入 SOC 并同步到单体，返回被替换掉的旧值。"""

        previous = self._soc_pct
        self._soc_pct = float(soc_pct)
        for cell in self.cells():
            cell.soc_pct = float(soc_pct)
        return previous

    def apply_temperature(self, temperature_c: float) -> float:
        """把温度采集写进簇内全部单体，返回写入值。"""

        for pack in self._packs:
            pack.apply_temperature(temperature_c)
        return float(temperature_c)

    def apply_pack_temperatures(self, readings: dict[str, float]) -> dict[str, float]:
        """按包写入温度采集。"""

        applied: dict[str, float] = {}
        for pack in self._packs:
            value = readings.get(pack.pack_id)
            if value is None:
                continue
            pack.apply_temperature(value)
            applied[pack.pack_id] = float(value)
        return applied

    def pack_temperatures(self) -> dict[str, float]:
        """返回每包最高单体温度。"""

        return {pack.pack_id: pack.hottest().temperature_c for pack in self._packs}

    def temperature_c(self) -> float:
        """返回簇内最高单体温度。"""

        return max(cell.temperature_c for cell in self.cells())

    def rating_v(self) -> float:
        """返回簇内最保守的当前额定值（混装时取最小额定值）。"""

        return min(pack.rating_v() for pack in self._packs)

    def is_mixed_rating(self) -> bool:
        """簇内是否存在不同代际额定值。"""

        return len({round(pack.rating_v(), 9) for pack in self._packs}) > 1

    def metrics(self) -> RackMetrics:
        """返回簇级度量。"""

        return compute_metrics(self.cells())

    def energy_kwh(self, capacity_ah: float) -> float:
        """按当前 SOC 折算簇内电量。"""

        return rack_energy_kwh(self.cells(), self._soc_pct, capacity_ah)

    def usable_energy_kwh(self, limits: RackLimits, capacity_ah: float) -> float:
        """按允许 SOC 窗口折算可用电量。"""

        return usable_energy_kwh(self.cells(), limits, capacity_ah)

    def replace_pack_rating(self, pack_index: int, rating_v: float, scope: str = "local") -> dict:
        """更换某包的整包电芯，返回变更记录。

        local 只改实际更换的包；cluster 是整簇统一重标，要求簇内最终额定值
        完全一致。两种口径的混装结果不同，记录里必须保留所选 scope。
        """

        if scope not in {"local", "cluster"}:
            raise ValueError("额定值刷新口径必须是 local 或 cluster")
        if not 0 <= pack_index < len(self._packs):
            raise IndexError(f"电池包序号越界: {pack_index}")
        before = {
            "rating_map": self.rating_map(),
            "rating_v": round(self.rating_v(), 4),
            "rating_epoch": self._rating_epoch,
            "mixed": self.is_mixed_rating(),
        }
        if scope == "cluster" and before["mixed"]:
            raise ValueError("混装簇不能整簇统一重标；请对实际更换的包使用 local")
        changes: list[dict] = []
        targets = range(len(self._packs)) if scope == "cluster" else [pack_index]
        for index in targets:
            pack = self._packs[index]
            change = pack.apply_rating(rating_v)
            change["pack_index"] = index
            changes.append(change)
        if scope == "cluster" and self.is_mixed_rating():
            raise ValueError("整簇统一重标后仍存在混装额定值")
        self._rating_v = self.rating_v()
        self._rating_epoch += 1
        return {
            "rack_id": self._rack_id,
            "pack_id": self._packs[pack_index].pack_id,
            "scope": scope,
            "selected_refresh_scope": scope,
            "reason": "mixed_cluster_uses_local" if self.is_mixed_rating() else "uniform_cluster",
            "previous_rating_v": round(changes[0]["previous_rating_v"], 4),
            "rating_v": round(float(rating_v), 4),
            "effective_rating_v": round(self.rating_v(), 4),
            "rating_epoch": self._rating_epoch,
            "cells": changes[0]["cells"],
            "changed_packs": changes,
            "rating_map": self.rating_map(),
            "mixed": self.is_mixed_rating(),
            "before": before,
        }

    def snapshot(self) -> RackSnapshot:
        """返回簇状态快照。"""

        return RackSnapshot(
            rack_id=self._rack_id,
            zone=self._zone,
            soc_pct=self._soc_pct,
            rating_epoch=self._rating_epoch,
            rating_map=self.rating_map(),
            mixed_rating=self.is_mixed_rating(),
            metrics=self.metrics(),
        )
