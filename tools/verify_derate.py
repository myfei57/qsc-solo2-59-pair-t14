"""手工验证脚本：在没有 pytest 的环境里等效回放测试场景。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from batterystore.app import build_platform  # noqa: E402
from batterystore.clock import ManualClock  # noqa: E402
from batterystore.config import default_config  # noqa: E402
from batterystore.temp.baseline import TempBaseline  # noqa: E402
from support import bring_online, connect_grid, prepare_charge  # noqa: E402

PASS = []


def check(name, condition, info=""):
    if not condition:
        raise AssertionError(f"{name} 失败 {info}")
    PASS.append(name)


def new_platform():
    clock = ManualClock()
    tmp = tempfile.mkdtemp(prefix="verify-state-")
    return build_platform(default_config(), clock=clock, storage_root=tmp, rack_count=2), clock


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def main() -> int:
    platform, clock = new_platform()

    # --- 曲线与冷却原语 -------------------------------------------------
    curve = platform.temp.curve
    limits = platform.config.temp
    check("curve.low", curve.ratio(limits.derate_start_c - 1.0) == 1.0)
    check("curve.floor", curve.ratio(limits.derate_full_c + 1.0) == limits.derate_floor_ratio)
    cooler = platform.temp.cooler
    check("cooler.idle", cooler.tick(5.0, 40.0) == 40.0)
    cooler.start()
    check("cooler.run", cooler.tick(5.0, 40.0) < 40.0)

    # --- 基线：换额定值立即过期 ------------------------------------------
    rack = platform.racks["rk-01"]
    first = platform.temp.apply_temperature(38.0, clock.now(), rack.pack_ratings())
    check("baseline.first_refresh", first["baseline_refreshed"] is True)
    check("baseline.all_packs", len(first["refreshed_packs"]) == len(rack.packs()))
    check("baseline.epoch1", platform.temp.baseline("rk-01-p1").epoch == 1)
    rack.replace_pack_rating(0, 3.35)
    report = platform.temp.baseline_report(clock.now(), rack.pack_ratings())
    check("baseline.stale_rating", report["packs"]["rk-01-p1"]["reason"] == "rating_changed")
    check("baseline.other_fresh", report["packs"]["rk-01-p2"]["stale"] is False)
    second = platform.temp.apply_temperature(38.0, clock.now(), rack.pack_ratings())
    check("baseline.local_only", [i["pack_id"] for i in second["refreshed_packs"]] == ["rk-01-p1"])
    check("baseline.epoch2", platform.temp.baseline("rk-01-p1").epoch == 2)
    check("baseline.other_untouched", platform.temp.baseline("rk-01-p2").epoch == 1)
    check("baseline.other_rating", platform.temp.baseline("rk-01-p2").rating_v == 3.2)
    check("rack.conservative_rating", rack.rating_v() == 3.2)

    # --- 基线历史：前后对照 ----------------------------------------------
    history = platform.temp.baseline("rk-01-p1").history()
    check("history.len", len(history) == 2)
    check("history.first_reason", history[0]["reason"] == "uninitialised")
    check("history.before_after", history[1]["previous_rating_v"] == 3.2 and history[1]["rating_v"] == 3.35)
    check("history.epochs", history[1]["previous_rating_epoch"] == 0 and history[1]["rating_epoch"] == 1)
    check("history.other_single", len(platform.temp.baseline("rk-01-p2").history()) == 1)

    # --- TTL 过期 ---------------------------------------------------------
    clock.advance(limits.derate_ttl_s if hasattr(limits, "derate_ttl_s") else limits.baseline_ttl_s + 1.0)
    report = platform.temp.baseline_report(clock.now(), rack.pack_ratings())
    check("baseline.expired", report["packs"]["rk-01-p1"]["reason"] == "expired")
    check("limit.eq_ratio_after_expiry", approx(platform.temp.charge_limit_ratio(), platform.temp.ratio()))

    # --- 未标定包压整簇 ----------------------------------------------------
    platform2, clock2 = new_platform()
    check("limit.floor_when_empty", platform2.temp.charge_limit_ratio() == platform2.config.temp.derate_floor_ratio)
    rack2 = platform2.racks["rk-01"]
    platform2.temp.apply_temperature(30.0, clock2.now(), rack2.pack_ratings())
    check("limit.full_when_fresh", platform2.temp.charge_limit_ratio() == 1.0)
    platform2.temp.baseline("rk-01-p9")
    detail = platform2.temp.charge_limit_detail()
    check("limit.strictest", detail["ratio"] == platform2.config.temp.derate_floor_ratio)
    check("limit.names_pack", detail["limiting_pack"] == "rk-01-p9" and detail["baseline_epoch"] == 0)

    # --- 平台级：三处同一切换 ---------------------------------------------
    platform3, _ = new_platform()
    platform3.start()
    platform3.sample_temperature(temperature_c=50.0, dt=5.0)
    check("pcs.derated", platform3.pcs.derate.ratio < 1.0)
    check("pcs.cooling", platform3.temp.cooler.is_running() is True)
    replaced = platform3.replace_pack_rating(pack_index=0, rating_v=3.4)
    check("replace.ok", replaced["ok"])
    actions = [step["action"] for step in replaced["steps"]]
    check("replace.steps", actions == ["replace_pack", "baseline_refresh", "derate_sync"], actions)
    check("replace.sync_ratio", replaced["after"]["pcs_derate_ratio"] == replaced["after"]["charge_limit_ratio"])
    check("replace.cooling_kept", replaced["after"]["cooling"] is True)
    check("replace.pcs_follows", platform3.pcs.derate.sources["temperature"] == platform3.temp.charge_limit_ratio())
    check("replace.meta_pack", platform3.pcs.derate.meta["limiting_pack"] == "rk-01-p1")
    check("replace.meta_epoch", platform3.pcs.derate.meta["baseline_epoch"] == platform3.temp.baseline("rk-01-p1").epoch)
    check("replace.before_kept", replaced["before"]["pcs_derate_ratio"] < 1.0)
    events = platform3.audit.query(component="pcs", action="derate_sync")
    check("audit.derate_sync", len(events) == 1)
    payload = events[0]["payload"]
    check("audit.epoch", payload["baseline_epoch"] == platform3.temp.baseline("rk-01-p1").epoch)
    check("audit.previous", payload["previous_ratio"] == replaced["before"]["pcs_derate_ratio"])

    # --- 正常温度换包：三处都放开 -----------------------------------------
    platform4, _ = new_platform()
    platform4.start()
    platform4.sample_temperature(temperature_c=30.0, dt=5.0)
    released = platform4.replace_pack_rating(pack_index=0, rating_v=3.4)
    check("release.ratio", released["after"]["charge_limit_ratio"] == 1.0)
    check("release.pcs", released["after"]["pcs_derate_ratio"] == 1.0)
    check("release.cooling", released["after"]["cooling"] is False)
    check("release.pcs_state", platform4.pcs.derate.ratio == 1.0)

    # --- 既有端到端链路 ----------------------------------------------------
    platform5, _ = new_platform()
    bring_online(platform5)
    connect_grid(platform5)
    prepare_charge(platform5)
    charge = platform5.charge(steps=3, dt=600.0)
    check("charge.ok", charge["ok"], charge["violations"])
    derate_step = [s for s in charge["steps"] if s["action"] == "derate"][0]
    check("charge.epoch_in_step", "baseline_epoch" in derate_step["detail"], derate_step)
    discharge = platform5.discharge(power_kw=300.0, dt=600.0)
    check("discharge.ok", discharge["ok"], discharge["violations"])
    cycle = platform5.cycle()
    check("cycle.runs", cycle["cycle"] == 1)
    check("cycle.pcs_synced", "pcs_derate_ratio" in cycle and "baseline_epoch" in cycle)
    check("cycle.invariant", platform5.pcs.derate.sources["temperature"] == platform5.temp.charge_limit_ratio())

    # --- 快照与控制台面 ----------------------------------------------------
    snap = platform5.temp_snapshot()
    check("snapshot.packs", "rk-01-p1" in snap["baseline"]["packs"])
    check("snapshot.limit", snap["baseline"]["limit"]["baseline_epoch"] >= 1)
    pcs_snap = platform5.pcs_snapshot()
    check("pcs.snapshot_meta", "derate_meta" in pcs_snap)
    status = platform5.status()
    check("status.ok", status["station"] == "station1")

    # --- TempBaseline 原语 -------------------------------------------------
    baseline = TempBaseline(platform5.config.temp.baseline_ttl_s)
    check("primitive.uninit", baseline.stale(0, 0.0) is True)
    try:
        baseline.refresh(0.0, 0, 0.0)
        check("primitive.reject_zero", False)
    except ValueError:
        check("primitive.reject_zero", True)
    baseline.refresh(3.2, 0, 0.0)
    check("primitive.fresh", baseline.latest(0.0, 0)["reason"] == "fresh")

    print(f"全部通过：{len(PASS)} 项检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
