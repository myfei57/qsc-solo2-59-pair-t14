"""簇状态快照。"""

from __future__ import annotations

from dataclasses import dataclass

from .metrics import RackMetrics


@dataclass(frozen=True)
class RackSnapshot:
    """一簇电池某一时刻的状态。"""

    rack_id: str
    zone: str
    soc_pct: float
    rating_epoch: int
    rating_map: dict
    mixed_rating: bool
    metrics: RackMetrics

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "zone": self.zone,
            "soc_pct": round(self.soc_pct, 2),
            "rating_epoch": self.rating_epoch,
            "rating_map": dict(self.rating_map),
            "mixed_rating": self.mixed_rating,
            "metrics": self.metrics.as_dict(),
        }
