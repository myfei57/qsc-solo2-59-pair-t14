"""电池包。

一个电池包是一串单体串联。包电压是串联单体电压之和，包之间的差异比单体差异
更能说明问题：某包整体偏低说明该串里有落后单体，需要在均衡阶段优先处理。
"""

from __future__ import annotations

from .cells import Cell


class Pack:
    """串联单体组成的电池包。"""

    def __init__(self, pack_id: str, cells: list[Cell]) -> None:
        if not cells:
            raise ValueError("电池包不能为空")
        self._pack_id = pack_id
        self._cells = list(cells)
        self._rating_epoch = 0

    @property
    def pack_id(self) -> str:
        return self._pack_id

    @property
    def rating_epoch(self) -> int:
        """返回包级额定值代际，整包换电芯加一。"""

        return self._rating_epoch

    def rating_v(self) -> float:
        """返回包当前额定值（整包为同规格电芯）。"""

        return self._cells[0].rating_v

    def cells(self) -> list[Cell]:
        """返回包内单体（按注册顺序）。"""

        return list(self._cells)

    def voltage_v(self) -> float:
        """返回包电压。"""

        return sum(cell.voltage_v for cell in self._cells)

    def weakest(self) -> Cell:
        """返回电压最低的单体。"""

        return min(self._cells, key=lambda cell: cell.voltage_v)

    def hottest(self) -> Cell:
        """返回温度最高的单体。"""

        return max(self._cells, key=lambda cell: cell.temperature_c)

    def apply_temperature(self, temperature_c: float) -> None:
        """把采集温度写进包内全部单体。"""

        for cell in self._cells:
            cell.update(temperature_c=float(temperature_c))

    def apply_rating(self, rating_v: float) -> list[float]:
        """整包更换同规格电芯，返回每个单体被替换掉的旧额定值。"""

        previous = [cell.apply_rating(rating_v) for cell in self._cells]
        self._rating_epoch += 1
        return previous

    def rating_view(self) -> dict:
        """返回温控基线绑定所需的额定值视图（包标识 + 额定值 + 代际）。"""

        return {
            "pack_id": self._pack_id,
            "rating_v": round(self.rating_v(), 4),
            "rating_epoch": self._rating_epoch,
        }

    def as_dict(self) -> dict:
        return {
            "pack_id": self._pack_id,
            "cells": len(self._cells),
            "voltage_v": round(self.voltage_v(), 3),
            "weakest_cell": self.weakest().cell_id,
            "hottest_cell": self.hottest().cell_id,
        }
