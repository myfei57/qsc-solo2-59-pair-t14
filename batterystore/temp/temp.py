"""温控组件。

``TempControl`` 负责一条完整的降额链：采集温度 → 核对额定值基线是否最新 →
按曲线算系数 → 启动冷却。混装簇按包保存基线，并在一次提交里同时给出降额、
冷却和充电电流上限，PCS 只消费提交后的完整策略，不能只切其中一项。

换电芯默认采用"局部重标"：整簇统一重标会给没换过的旧包套上新电芯曲线，
在混装簇里产生偏宽松的系数；局部重标保留旧包的保守边界，再取簇内最严包，
才不会让旧电芯成为被新曲线掩盖的短板。
"""

from __future__ import annotations

from copy import deepcopy

from ..config import TempLimits
from ..pcs.derate import combine_ratios
from .baseline import ThermalProfile, TempBaseline, profile_for_rating
from .control import Cooler
from .curve import DerateCurve
from .snapshot import TempSnapshot


class TempControl:
    """温控与降额组件。"""

    def __init__(
        self,
        limits: TempLimits,
        *,
        reference_rating_v: float | None = None,
        configured_current_max_a: float | None = None,
        absolute_full_c: float | None = None,
    ) -> None:
        self._limits = limits
        self._reference_rating_v = float(reference_rating_v) if reference_rating_v is not None else None
        self._configured_current_max_a = (
            float(configured_current_max_a) if configured_current_max_a is not None else None
        )
        self._absolute_full_c = float(absolute_full_c) if absolute_full_c is not None else None
        self._curve = DerateCurve(limits)
        self._baseline = TempBaseline(limits.baseline_ttl_s)
        self._pack_baselines: dict[str, TempBaseline] = {}
        self._pack_temperatures: dict[str, float] = {}
        self._pack_ratings: dict[str, dict] = {}
        self._cooler = Cooler(limits.cool_rate_c_per_s)
        self._temperature_c = 25.0
        self._last_now = 0.0
        self._refreshes = 0
        self._stale_hits = 0
        self._policy_epoch = 0
        self._policy: dict | None = None
        self._policy_history: list[dict] = []

    @property
    def limits(self) -> TempLimits:
        return self._limits

    @property
    def curve(self) -> DerateCurve:
        return self._curve

    @property
    def baseline(self) -> TempBaseline:
        return self._baseline

    @property
    def cooler(self) -> Cooler:
        return self._cooler

    @property
    def policy_epoch(self) -> int:
        return self._policy_epoch

    def temperature_c(self) -> float:
        """返回当前温度。"""

        return self._temperature_c

    def profile_for_rating(self, rating_v: float) -> ThermalProfile:
        """返回某代电芯对应的温控／电流策略。"""

        return profile_for_rating(
            rating_v,
            self._limits,
            reference_rating_v=self._reference_rating_v,
            absolute_full_c=self._absolute_full_c,
        )

    def _profile(self) -> ThermalProfile | None:
        if self._pack_baselines:
            profiles = [baseline.profile for baseline in self._pack_baselines.values() if baseline.profile is not None]
            if not profiles:
                return None
            governing = self._governing_entry()[1]
            return governing.profile
        if self._baseline.profile is not None:
            return self._baseline.profile
        return None

    def governing_profile(self) -> ThermalProfile | None:
        """返回簇内当前决定降额系数的那一代 profile。"""

        return self._profile()

    def ratio(self, temperature_c: float | None = None) -> float:
        """返回当前温度下的降额系数。"""

        value = self._temperature_c if temperature_c is None else float(temperature_c)
        return self._curve.ratio(value, self._profile())

    def charge_limit_ratio(self) -> float:
        """返回充电限制系数：基线过期时按最严系数处理。"""

        if self._policy is not None:
            if self._pack_baselines and any(
                baseline.stale(self._pack_ratings[pack_id]["rating_epoch"], self._last_now)
                for pack_id, baseline in self._pack_baselines.items()
            ):
                return self._limits.derate_floor_ratio
            return float(self._policy["charge_limit_ratio"])
        if self._pack_baselines:
            return self._limits.derate_floor_ratio
        if self._baseline.epoch == 0:
            return self._limits.derate_floor_ratio
        if self._baseline.stale(self._baseline.rating_epoch, self._last_now):
            return self._limits.derate_floor_ratio
        return self.ratio()

    def charge_current_max_a(self) -> float | None:
        """返回当前策略约束下的充电电流上限。"""

        if self._policy is None or self._configured_current_max_a is None:
            return None
        return float(self._policy["charge_current_max_a"])

    def _refresh_legacy_baseline(self, rating_v: float, rating_epoch: int, now: float, reason: str) -> dict:
        previous = self._baseline.latest(now, rating_epoch)
        record = self._baseline.refresh(
            rating_v,
            rating_epoch,
            now,
            profile=self.profile_for_rating(rating_v),
            reason=reason,
            before=None if previous["epoch"] == 0 else previous,
        )
        record["before"] = None if previous["epoch"] == 0 else previous
        return record

    def _cooling_bounds(self, profile: ThermalProfile) -> tuple[float, float]:
        return profile.cooling_start_c, profile.cooling_stop_c

    def _control_cooler(self, temperature_c: float, profile: ThermalProfile) -> bool:
        start_c, stop_c = self._cooling_bounds(profile)
        if temperature_c >= start_c:
            self._cooler.start()
        elif temperature_c <= stop_c:
            self._cooler.stop()
        return self._cooler.is_running()

    def apply_temperature(self, temperature_c: float, now: float, rating_v: float, rating_epoch: int) -> dict:
        """写入温度采集，必要时重标基线，返回本次采集结果（兼容单簇接口）。"""

        self._temperature_c = float(temperature_c)
        self._last_now = float(now)
        refreshed = False
        refresh_record = None
        if self._baseline.stale(rating_epoch, now):
            self._stale_hits += 1
            refresh_record = self._refresh_legacy_baseline(rating_v, rating_epoch, now, "rating_changed")
            self._refreshes += 1
            refreshed = True
        profile = self._baseline.profile
        cooling = self._control_cooler(self._temperature_c, profile)
        result = {
            "temperature_c": round(self._temperature_c, 2),
            "ratio": round(self._curve.ratio(self._temperature_c, profile), 4),
            "band": self._curve.band(self._temperature_c, profile),
            "baseline_refreshed": refreshed,
            "refresh": refresh_record,
            "generation": profile.generation if profile else "unknown",
            "cooling": cooling,
            "cooling_start_c": round(profile.cooling_start_c, 3) if profile else self._limits.derate_start_c,
            "cooling_stop_c": round(profile.cooling_stop_c, 3) if profile else self._limits.derate_start_c - 5.0,
        }
        return result

    def _ensure_pack(self, pack_id: str, rating_v: float, rating_epoch: int) -> TempBaseline:
        baseline = self._pack_baselines.get(pack_id)
        if baseline is None:
            baseline = TempBaseline(self._limits.baseline_ttl_s, target_id=pack_id, scope="local")
            self._pack_baselines[pack_id] = baseline
        self._pack_ratings[pack_id] = {"rating_v": float(rating_v), "rating_epoch": int(rating_epoch)}
        return baseline

    def _governing_entry(self) -> tuple[str, TempBaseline]:
        candidates: list[tuple[float, float, str, TempBaseline]] = []
        for pack_id, baseline in self._pack_baselines.items():
            temperature = float(self._pack_temperatures.get(pack_id, self._temperature_c))
            profile = baseline.profile
            ratio = self._curve.ratio(temperature, profile)
            if profile is None:
                ratio = self._limits.derate_floor_ratio
            candidates.append((ratio, temperature, pack_id, baseline))
        _, _, pack_id, baseline = min(candidates, key=lambda item: (item[0], -item[1], item[2]))
        return pack_id, baseline

    def _build_policy(self, now: float, scope: str, refreshed_packs: list[dict], reason: str) -> dict:
        pack_results: dict[str, dict] = {}
        for pack_id, baseline in self._pack_baselines.items():
            temperature = float(self._pack_temperatures.get(pack_id, self._temperature_c))
            profile = baseline.profile
            ratio = self._curve.ratio(temperature, profile)
            if profile is None:
                ratio = self._limits.derate_floor_ratio
            current_ratio = profile.charge_current_ratio if profile is not None else 0.0
            pack_results[pack_id] = {
                "temperature_c": round(temperature, 3),
                "ratio": round(ratio, 4),
                "charge_current_ratio": round(current_ratio, 4),
                "generation": profile.generation if profile else "unknown",
                "rating_epoch": baseline.latest(now, self._pack_ratings[pack_id]["rating_epoch"])["rating_epoch"],
                "baseline_epoch": baseline.epoch,
            }
        governing_id, governing = self._governing_entry()
        profile = governing.profile
        if profile is None:
            raise ValueError("温控策略至少需要一份已刷新基线")
        charge_ratio = min(item["ratio"] for item in pack_results.values())
        current_ratio = min(item["charge_current_ratio"] for item in pack_results.values())
        cooling_start = min(
            baseline.profile.cooling_start_c
            for baseline in self._pack_baselines.values()
            if baseline.profile is not None
        )
        cooling_stop = min(
            baseline.profile.cooling_stop_c
            for baseline in self._pack_baselines.values()
            if baseline.profile is not None
        )
        cooling_required = any(
            float(self._pack_temperatures.get(pack_id, self._temperature_c))
            >= (baseline.profile.cooling_start_c if baseline.profile else cooling_start)
            for pack_id, baseline in self._pack_baselines.items()
        )
        if cooling_required:
            self._cooler.start()
        elif all(
            float(self._pack_temperatures.get(pack_id, self._temperature_c))
            <= (baseline.profile.cooling_stop_c if baseline.profile else cooling_stop)
            for pack_id, baseline in self._pack_baselines.items()
        ):
            self._cooler.stop()
        current_limit = (
            round(self._configured_current_max_a * current_ratio * charge_ratio, 3)
            if self._configured_current_max_a is not None
            else None
        )
        generations = sorted({item["generation"] for item in pack_results.values()})
        return {
            "policy_epoch": self._policy_epoch + 1,
            "reason": reason,
            "scope": scope,
            "selected_refresh_scope": scope,
            "selection_reason": "uniform_cluster" if len(generations) == 1 else "mixed_cluster_local_pack",
            "governing_pack_id": governing_id,
            "generation": profile.generation,
            "generations": generations,
            "mixed_cluster": len(generations) > 1,
            "charge_limit_ratio": round(charge_ratio, 4),
            "charge_current_ratio": round(current_ratio, 4),
            "charge_current_max_a": current_limit,
            "cooling_required": self._cooler.is_running(),
            "cooling_start_c": round(cooling_start, 3),
            "cooling_stop_c": round(cooling_stop, 3),
            "packs": pack_results,
            "refreshed_packs": refreshed_packs,
            "stamped_at": round(float(now), 3),
        }

    def _commit_policy(self, policy: dict) -> dict:
        self._policy_epoch = int(policy["policy_epoch"])
        committed = deepcopy(policy)
        committed.pop("previous_policy", None)
        self._policy = committed
        self._policy_history.append(deepcopy(committed))
        return deepcopy(committed)

    def apply_pack_temperatures(
        self,
        readings: dict[str, tuple[float, float, int]],
        now: float,
        *,
        scope: str = "local",
        reason: str = "temperature_sample",
    ) -> dict:
        """按包采集温度并返回簇级热策略。

        readings: pack_id -> (temperature_c, rating_v, rating_epoch)
        局部重标只刷新代际变化的包；整簇重标仅在全簇同代际时允许。
        """

        if scope not in {"local", "cluster"}:
            raise ValueError("基线刷新口径必须是 local 或 cluster")
        if not readings:
            raise ValueError("至少需要一个电池包温度")

        ratings = {pack_id: float(item[1]) for pack_id, item in readings.items()}
        if scope == "cluster" and len(set(round(value, 9) for value in ratings.values())) > 1:
            raise ValueError("混装簇不能按整簇统一重标，必须使用 local 局部重标")

        self._temperature_c = max(float(item[0]) for item in readings.values())
        self._last_now = float(now)
        for pack_id, (temperature_c, rating_v, rating_epoch) in readings.items():
            self._pack_temperatures[pack_id] = float(temperature_c)
            baseline = self._ensure_pack(pack_id, rating_v, rating_epoch)

        refreshed_packs: list[dict] = []
        for pack_id, (_, rating_v, rating_epoch) in readings.items():
            baseline = self._pack_baselines[pack_id]
            is_stale = baseline.stale(rating_epoch, now)
            should_refresh = scope == "cluster" or is_stale
            if is_stale:
                self._stale_hits += 1
            if should_refresh:
                before = baseline.latest(now, rating_epoch)
                profile = self.profile_for_rating(rating_v)
                record = baseline.refresh(
                    rating_v,
                    rating_epoch,
                    now,
                    profile=profile,
                    scope=scope,
                    target_id=pack_id,
                    reason="rating_changed" if is_stale else reason,
                    before=None if before["epoch"] == 0 else before,
                )
                record["before"] = None if before["epoch"] == 0 else before
                refreshed_packs.append(record)
                self._refreshes += 1

        policy = self._build_policy(now, scope, refreshed_packs, reason)
        policy["previous_policy"] = deepcopy(self._policy)
        self._commit_policy(policy)
        self.sync_scalar_baseline(now, refreshed_packs)
        return deepcopy(policy)

    def activate_ratings(
        self,
        pack_ratings: dict[str, tuple[float, int]],
        now: float,
        *,
        scope: str = "local",
        reason: str = "rating_activated",
    ) -> dict:
        """新额定值生效后立刻重算，不等下一次温度采集。"""

        readings: dict[str, tuple[float, float, int]] = {}
        for pack_id, (rating_v, rating_epoch) in pack_ratings.items():
            temperature = float(self._pack_temperatures.get(pack_id, self._temperature_c))
            readings[pack_id] = (temperature, rating_v, rating_epoch)
        return self.apply_pack_temperatures(readings, now, scope=scope, reason=reason)

    def sync_scalar_baseline(self, now: float, refreshed_packs: list[dict] | None = None) -> dict:
        """同步兼容旧接口的单基线视图。

        局部重标后该视图指向最新重标的包；簇内真正生效的最严代际仍以 policy
        的 governing_pack_id/generation 为准。
        """

        if refreshed_packs:
            pack_id = refreshed_packs[0]["target_id"]
            pack_baseline = self._pack_baselines[pack_id]
        else:
            pack_id, pack_baseline = self._governing_entry()
        profile = pack_baseline.profile
        if profile is None:
            return {}
        if (
            self._baseline.target_id == pack_id
            and self._baseline.rating_epoch == pack_baseline.rating_epoch
            and self._baseline.rating_v == profile.rating_v
        ):
            return self._baseline.latest(now, pack_baseline.rating_epoch)
        alias_before = None if self._baseline.epoch == 0 else self._baseline.latest(now, self._baseline.rating_epoch)
        self._baseline.refresh(
            profile.rating_v,
            pack_baseline.rating_epoch,
            now,
            profile=profile,
            target_id=pack_id,
            scope="pack-alias",
            reason="governing_pack_alias",
            before=alias_before,
        )
        return self._baseline.latest(now, pack_baseline.rating_epoch)

    def cool(self, dt: float) -> float:
        """推进一个冷却步长，返回冷却后的温度。"""

        self._temperature_c = self._cooler.tick(dt, self._temperature_c)
        for pack_id in self._pack_temperatures:
            self._pack_temperatures[pack_id] = self._temperature_c
        return self._temperature_c

    def combined_ratio(self, **ratios: float) -> dict:
        """把温控系数与其它系数一起合成。"""

        return combine_ratios(temperature=self.charge_limit_ratio(), **ratios).as_dict()

    def baseline_report(self, now: float, rating_epoch: int) -> dict:
        """返回基线现状与重标计数。"""

        report = self._baseline.latest(now, rating_epoch)
        report["refreshes"] = self._refreshes
        report["stale_hits"] = self._stale_hits
        report["selected_refresh_scope"] = self._policy["selected_refresh_scope"] if self._policy else "local"
        report["policy_epoch"] = self._policy_epoch
        report["policy_history"] = self.policy_history()
        if self._pack_baselines:
            report["packs"] = {
                pack_id: baseline.latest(now, self._pack_ratings[pack_id]["rating_epoch"])
                for pack_id, baseline in self._pack_baselines.items()
            }
            report["generation"] = self._policy["generation"] if self._policy else "unknown"
        return report

    def policy(self) -> dict | None:
        """返回最近一次原子提交的热策略。"""

        return None if self._policy is None else deepcopy(self._policy)

    def policy_history(self) -> list[dict]:
        """返回每次刷新前后完整策略记录。"""

        return deepcopy(self._policy_history)

    def snapshot(self, now: float, rating_epoch: int) -> TempSnapshot:
        """返回温控状态快照。"""

        profile = self._profile()
        return TempSnapshot(
            temperature_c=self._temperature_c,
            ratio=self.ratio(),
            charge_limit_ratio=self.charge_limit_ratio(),
            band=self._curve.band(self._temperature_c, profile),
            cooling=self._cooler.is_running(),
            cooler_ticks=self._cooler.ticks(),
            removed_c=self._cooler.removed_c(),
            baseline=self.baseline_report(now, rating_epoch),
        )
