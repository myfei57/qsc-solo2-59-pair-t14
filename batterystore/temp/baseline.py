"""温控降额基线。

降额基线绑定"当前额定值 + 额定值代际"。换电芯后额定值代际变化，基线立即
判为过期，必须重新标定；没有这条判据就会出现"换完电芯还按旧额定值算降额"，
正常温度被误判成高温。每次重标都留一条前后对照记录（旧额定值、旧代际、
新额定值、新代际、触发原因），回溯"当时为什么压着功率"时不用猜。
"""

from __future__ import annotations


class TempBaseline:
    """带额定值代际、有效期与重标历史的降额基线。"""

    def __init__(self, ttl_s: float) -> None:
        if ttl_s <= 0:
            raise ValueError("基线有效期必须为正")
        self._ttl_s = float(ttl_s)
        self._rating_v = 0.0
        self._rating_epoch = -1
        self._stamped_at = 0.0
        self._epoch = 0
        self._history: list[dict] = []

    @property
    def epoch(self) -> int:
        """返回基线代际，重新标定一次加一。"""

        return self._epoch

    @property
    def rating_v(self) -> float:
        """返回基线绑定的额定值。"""

        return self._rating_v

    @property
    def rating_epoch(self) -> int:
        """返回基线绑定的额定值代际。"""

        return self._rating_epoch

    def refresh(self, rating_v: float, rating_epoch: int, now: float, reason: str = "rating_changed") -> dict:
        """按最新额定值重新标定基线，返回前后对照的标定记录。"""

        if rating_v <= 0:
            raise ValueError("额定值必须为正")
        entry = {
            "epoch": self._epoch + 1,
            "rating_v": round(float(rating_v), 4),
            "rating_epoch": int(rating_epoch),
            "previous_epoch": self._epoch,
            "previous_rating_v": round(self._rating_v, 4),
            "previous_rating_epoch": self._rating_epoch,
            "stamped_at": round(float(now), 3),
            "reason": str(reason),
        }
        self._rating_v = float(rating_v)
        self._rating_epoch = int(rating_epoch)
        self._stamped_at = float(now)
        self._epoch += 1
        self._history.append(entry)
        return dict(entry)

    def stale(self, rating_epoch: int, now: float) -> bool:
        """基线是否已过期：额定值代际变化或超过有效期。"""

        if self._epoch == 0:
            return True
        if int(rating_epoch) != self._rating_epoch:
            return True
        return max(0.0, float(now) - self._stamped_at) > self._ttl_s

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
            "rating_v": round(self._rating_v, 4),
            "rating_epoch": self._rating_epoch,
            "age_s": round(age, 2),
            "ttl_s": self._ttl_s,
            "stale": stale,
            "reason": reason,
        }

    def history(self, limit: int = 20) -> list[dict]:
        """返回最近若干次重标记录（前后对照）。"""

        return [dict(item) for item in self._history[-limit:]]
