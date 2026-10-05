"""温控降额基线。

降额基线绑定"当前额定值 + 额定值代际"。换电芯后额定值代际变化，基线立即
判为过期，必须重新标定；没有这条判据就会出现"换完电芯还按旧额定值算降额"，
正常温度被误判成高温。

基线是只追加的标定账本：重标前先把旧策略复制到 history，审计可以同时回答
"刚才为什么压功率"和"新策略是从哪一代切过来的"。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class ThermalProfile:
    """一代电芯对应的温控／充电策略。"""

    generation: str
    rating_v: float
    derate_start_c: float
    derate_full_c: float
    derate_floor_ratio: float
    cooling_start_c: float
    cooling_stop_c: float
    charge_current_ratio: float

    def as_dict(self) -> dict:
        return {
            "generation": self.generation,
            "rating_v": round(self.rating_v, 4),
            "derate_start_c": round(self.derate_start_c, 3),
            "derate_full_c": round(self.derate_full_c, 3),
            "derate_floor_ratio": round(self.derate_floor_ratio, 4),
            "cooling_start_c": round(self.cooling_start_c, 3),
            "cooling_stop_c": round(self.cooling_stop_c, 3),
            "charge_current_ratio": round(self.charge_current_ratio, 4),
        }


def profile_for_rating(
    rating_v: float,
    limits,
    *,
    reference_rating_v: float | None = None,
    absolute_full_c: float | None = None,
) -> ThermalProfile:
    """按额定值代际生成温控策略。

    以现场初始电芯为基准代际。新一代高额定值电芯允许更高温度门槛，冷却和降额
    相应后移；恒功率下电流上限随电压升高按比例下降。三个量必须来自同一份
    profile，禁止各处各算。
    """

    value = float(rating_v)
    if value <= 0:
        raise ValueError("额定值必须为正")
    reference = float(reference_rating_v) if reference_rating_v is not None else getattr(limits, "cell_nominal_v", 3.2)
    if reference <= 0:
        raise ValueError("基准额定值必须为正")

    thermal_scale = value / reference
    current_scale = min(1.0, reference / value)
    derate_start = limits.derate_start_c * thermal_scale
    derate_full = limits.derate_full_c * thermal_scale
    if absolute_full_c is not None:
        derate_full = min(derate_full, float(absolute_full_c))
        derate_start = min(derate_start, derate_full - 1.0)
    generation = f"rating-{value:.4f}v"
    return ThermalProfile(
        generation=generation,
        rating_v=value,
        derate_start_c=derate_start,
        derate_full_c=derate_full,
        derate_floor_ratio=limits.derate_floor_ratio,
        cooling_start_c=derate_start,
        cooling_stop_c=max(0.0, derate_start - 5.0),
        charge_current_ratio=current_scale,
    )


@dataclass(frozen=True)
class BaselineStamp:
    """一次基线生效记录。"""

    epoch: int
    target_id: str
    scope: str
    rating_epoch: int
    stamped_at: float
    profile: ThermalProfile
    reason: str = "refresh"
    before: dict | None = None

    def as_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "target_id": self.target_id,
            "scope": self.scope,
            "rating_epoch": self.rating_epoch,
            "stamped_at": round(self.stamped_at, 3),
            "reason": self.reason,
            "before": deepcopy(self.before),
            "profile": self.profile.as_dict(),
        }


class TempBaseline:
    """带额定值代际与有效期的降额基线。"""

    def __init__(self, ttl_s: float, target_id: str = "cluster", scope: str = "cluster") -> None:
        if ttl_s <= 0:
            raise ValueError("基线有效期必须为正")
        self._ttl_s = float(ttl_s)
        self._target_id = target_id
        self._scope = scope
        self._rating_v = 0.0
        self._rating_epoch = -1
        self._stamped_at = 0.0
        self._epoch = 0
        self._profile: ThermalProfile | None = None
        self._history: list[BaselineStamp] = []

    @property
    def epoch(self) -> int:
        """返回基线代际，重新标定一次加一。"""

        return self._epoch

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def rating_v(self) -> float:
        """返回基线绑定的额定值。"""

        return self._rating_v

    @property
    def stamped_at(self) -> float:
        return self._stamped_at

    @property
    def profile(self) -> ThermalProfile | None:
        return self._profile

    @property
    def rating_epoch(self) -> int:
        """返回当前绑定的额定值代际。"""

        return self._rating_epoch

    @property
    def generation(self) -> str:
        """返回本次使用的电芯代际；未标定时显式返回 unknown。"""

        return self._profile.generation if self._profile else "unknown"

    def refresh(
        self,
        rating_v: float,
        rating_epoch: int,
        now: float,
        *,
        profile: ThermalProfile | None = None,
        scope: str | None = None,
        target_id: str | None = None,
        reason: str = "refresh",
        before: dict | None = None,
    ) -> dict:
        """按最新额定值重新标定基线，返回标定记录。"""

        if rating_v <= 0:
            raise ValueError("额定值必须为正")
        if profile is not None and abs(profile.rating_v - float(rating_v)) > 1e-9:
            raise ValueError("基线 profile 必须与额定值一致")
        self._epoch += 1
        if target_id is not None:
            self._target_id = target_id
        if scope is not None:
            self._scope = scope
        self._profile = profile or ThermalProfile(
            generation=f"rating-{float(rating_v):.4f}v",
            rating_v=float(rating_v),
            derate_start_c=0.0,
            derate_full_c=0.0,
            derate_floor_ratio=1.0,
            cooling_start_c=0.0,
            cooling_stop_c=0.0,
            charge_current_ratio=1.0,
        )
        self._rating_v = float(rating_v)
        self._rating_epoch = int(rating_epoch)
        self._stamped_at = float(now)
        stamp = BaselineStamp(
            epoch=self._epoch,
            target_id=self._target_id,
            scope=self._scope,
            rating_epoch=self._rating_epoch,
            stamped_at=self._stamped_at,
            profile=self._profile,
            reason=reason,
            before=deepcopy(before),
        )
        self._history.append(stamp)
        record = stamp.as_dict()
        record["rating_v"] = round(self._rating_v, 4)
        record["rating_epoch"] = self._rating_epoch
        record["stamped_at"] = round(self._stamped_at, 3)
        return record

    def stale(self, rating_epoch: int, now: float) -> bool:
        """基线是否已过期：额定值代际变化或超过有效期。"""

        if self._epoch == 0:
            return True
        if int(rating_epoch) != self._rating_epoch:
            return True
        return max(0.0, float(now) - self._stamped_at) > self._ttl_s

    def history(self) -> list[dict]:
        """返回只追加的重标记录，旧记录不会被新额定值覆盖。"""

        return [stamp.as_dict() for stamp in self._history]

    def previous(self) -> dict | None:
        """返回重标前一份基线；首次标定时为 None。"""

        if len(self._history) < 2:
            return None
        return self._history[-2].as_dict()

    def latest(self, now: float, rating_epoch: int) -> dict:
        """返回基线现状与过期原因。"""

        age = max(0.0, float(now) - self._stamped_at) if self._epoch else 0.0
        stale = self.stale(rating_epoch, now)
        if self._epoch == 0:
            reason = "uninitialised"
        elif int(rating_epoch) != self._rating_epoch:
            reason = "rating_changed"
        elif age > self._ttl_s:
            reason = "expired"
        else:
            reason = "fresh"
        return {
            "epoch": self._epoch,
            "target_id": self._target_id,
            "scope": self._scope,
            "generation": self.generation,
            "rating_v": round(self._rating_v, 4),
            "rating_epoch": self._rating_epoch,
            "age_s": round(age, 2),
            "ttl_s": self._ttl_s,
            "stale": stale,
            "reason": reason,
            "profile": self._profile.as_dict() if self._profile else None,
            "previous": self.previous(),
            "history_count": len(self._history),
        }
