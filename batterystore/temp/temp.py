"""温控组件。

``TempControl`` 负责一条完整的降额链：采集温度 → 逐包核对额定值基线是否
最新 → 按曲线算系数 → 启动冷却。基线按包维护（局部重标）：换过电芯的包
额定值代际变化，只有那一包的基线判过期并按新额定值重标，其余包的基线
不动；簇级充电限制系数取各包最严（串联簇电流处处相等，最严的包就是簇的
上限）。混装簇里绝不搞整簇统一重标——那会让没换的旧包也绑到新额定值
代际上，该压的压不住、不该压的乱压。
"""

from __future__ import annotations

from ..config import TempLimits
from ..pcs.derate import combine_ratios
from .baseline import TempBaseline
from .control import Cooler
from .curve import DerateCurve
from .snapshot import TempSnapshot


class TempControl:
    """温控与降额组件。"""

    def __init__(self, limits: TempLimits) -> None:
        self._limits = limits
        self._curve = DerateCurve(limits)
        self._baselines: dict[str, TempBaseline] = {}
        self._cooler = Cooler(limits.cool_rate_c_per_s)
        self._temperature_c = 25.0
        self._refreshes = 0
        self._stale_hits = 0

    @property
    def limits(self) -> TempLimits:
        return self._limits

    @property
    def curve(self) -> DerateCurve:
        return self._curve

    @property
    def cooler(self) -> Cooler:
        return self._cooler

    def baseline(self, pack_id: str) -> TempBaseline:
        """返回某包的降额基线，未登记的包现场登记（未标定状态）。"""

        key = str(pack_id)
        if key not in self._baselines:
            self._baselines[key] = TempBaseline(self._limits.baseline_ttl_s)
        return self._baselines[key]

    def temperature_c(self) -> float:
        """返回当前温度。"""

        return self._temperature_c

    def ratio(self) -> float:
        """返回当前温度下的降额系数。"""

        return self._curve.ratio(self._temperature_c)

    def charge_limit_ratio(self) -> float:
        """返回充电限制系数：逐包核对基线，簇级取最严。"""

        return self.charge_limit_detail()["ratio"]

    def charge_limit_detail(self) -> dict:
        """返回簇级充电限制系数及其来源：哪一包、哪一代基线在起限制作用。"""

        if not self._baselines:
            return {
                "ratio": round(self._limits.derate_floor_ratio, 4),
                "limiting_pack": None,
                "baseline_epoch": 0,
                "rating_epoch": -1,
            }
        limiting_pack: str | None = None
        limiting_ratio = 1.0
        for pack_id in sorted(self._baselines):
            baseline = self._baselines[pack_id]
            ratio = self._limits.derate_floor_ratio if baseline.epoch == 0 else self.ratio()
            if ratio < limiting_ratio:
                limiting_pack = pack_id
                limiting_ratio = ratio
        if limiting_pack is None:
            limiting_pack = sorted(self._baselines)[0]
        baseline = self._baselines[limiting_pack]
        return {
            "ratio": round(limiting_ratio, 4),
            "limiting_pack": limiting_pack,
            "baseline_epoch": baseline.epoch,
            "rating_epoch": baseline.rating_epoch,
        }

    def apply_temperature(self, temperature_c: float, now: float, packs: list[dict]) -> dict:
        """写入温度采集，逐包核对基线并按需重标，返回本次采集与刷新结果。"""

        self._temperature_c = float(temperature_c)
        refreshed: list[dict] = []
        epochs: dict[str, int] = {}
        for view in packs:
            pack_id = str(view["pack_id"])
            rating_epoch = int(view["rating_epoch"])
            baseline = self.baseline(pack_id)
            if baseline.stale(rating_epoch, now):
                self._stale_hits += 1
                reason = baseline.latest(now, rating_epoch)["reason"]
                record = baseline.refresh(float(view["rating_v"]), rating_epoch, now, reason)
                self._refreshes += 1
                refreshed.append({"pack_id": pack_id, **record})
            epochs[pack_id] = baseline.epoch
        cooling_before = self._cooler.is_running()
        if self._temperature_c >= self._limits.derate_start_c:
            self._cooler.start()
        elif self._temperature_c <= self._limits.derate_start_c - 5.0:
            self._cooler.stop()
        cooling = self._cooler.is_running()
        return {
            "temperature_c": round(self._temperature_c, 2),
            "ratio": round(self.ratio(), 4),
            "charge_limit_ratio": round(self.charge_limit_ratio(), 4),
            "band": self._curve.band(self._temperature_c),
            "baseline_refreshed": bool(refreshed),
            "refreshed_packs": refreshed,
            "baseline_epochs": epochs,
            "cooling": cooling,
            "cooling_switched": cooling != cooling_before,
        }

    def cool(self, dt: float) -> float:
        """推进一个冷却步长，返回冷却后的温度。"""

        self._temperature_c = self._cooler.tick(dt, self._temperature_c)
        return self._temperature_c

    def combined_ratio(self, **ratios: float) -> dict:
        """把温控系数与其它系数一起合成。"""

        return combine_ratios(temperature=self.charge_limit_ratio(), **ratios).as_dict()

    def baseline_report(self, now: float, packs: list[dict]) -> dict:
        """返回各包基线现状、当前起限制作用的基线代际与重标计数。"""

        views = {str(view["pack_id"]): int(view["rating_epoch"]) for view in packs}
        report_packs: dict[str, dict] = {}
        for pack_id in sorted(self._baselines):
            baseline = self._baselines[pack_id]
            report_packs[pack_id] = baseline.latest(now, views.get(pack_id, baseline.rating_epoch))
        for pack_id, rating_epoch in views.items():
            if pack_id not in report_packs:
                # 还没采集过的包按未标定展示（仅报表呈现，不参与限值合成）。
                report_packs[pack_id] = TempBaseline(self._limits.baseline_ttl_s).latest(now, rating_epoch)
        return {
            "packs": {pack_id: report_packs[pack_id] for pack_id in sorted(report_packs)},
            "limit": self.charge_limit_detail(),
            "refreshes": self._refreshes,
            "stale_hits": self._stale_hits,
        }

    def snapshot(self, now: float, packs: list[dict]) -> TempSnapshot:
        """返回温控状态快照。"""

        return TempSnapshot(
            temperature_c=self._temperature_c,
            ratio=self.ratio(),
            charge_limit_ratio=self.charge_limit_ratio(),
            band=self._curve.band(self._temperature_c),
            cooling=self._cooler.is_running(),
            cooler_ticks=self._cooler.ticks(),
            removed_c=self._cooler.removed_c(),
            baseline=self.baseline_report(now, packs),
        )
