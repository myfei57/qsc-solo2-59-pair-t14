"""温控降额与额定值基线。"""

from __future__ import annotations

import pytest

from batterystore.temp.baseline import TempBaseline


def test_cooler_only_cools_when_running(platform):
    cooler = platform.temp.cooler
    assert cooler.tick(5.0, 40.0) == 40.0
    cooler.start()
    cooled = cooler.tick(5.0, 40.0)
    assert cooled < 40.0
    assert cooler.is_running() is True
    assert cooler.ticks() == 2
    assert cooler.removed_c() > 0
    cooler.stop()
    assert cooler.is_running() is False
    with pytest.raises(ValueError):
        cooler.tick(0.0, 40.0)


def test_derate_curve_shape(platform):
    curve = platform.temp.curve
    limits = platform.config.temp
    assert curve.ratio(limits.derate_start_c - 1.0) == 1.0
    assert curve.ratio(limits.derate_full_c + 1.0) == limits.derate_floor_ratio
    middle = curve.ratio((limits.derate_start_c + limits.derate_full_c) / 2.0)
    assert limits.derate_floor_ratio < middle < 1.0
    assert curve.band(limits.derate_full_c) == "full_derate"
    assert curve.band(limits.warn_c + 1.0) == "derated"
    assert curve.band(0.0) == "normal"
    assert len(curve.points()) == 2


def test_baseline_refreshes_after_rating_change(platform, clock):
    rack = platform.racks["rk-01"]
    first = platform.temp.apply_temperature(38.0, clock.now(), rack.pack_ratings())
    assert first["baseline_refreshed"] is True
    assert len(first["refreshed_packs"]) == len(rack.packs())
    assert platform.temp.baseline("rk-01-p1").epoch == 1
    rack.replace_pack_rating(0, 3.35)
    report = platform.temp.baseline_report(clock.now(), rack.pack_ratings())
    assert report["packs"]["rk-01-p1"]["stale"] is True
    assert report["packs"]["rk-01-p1"]["reason"] == "rating_changed"
    assert report["packs"]["rk-01-p2"]["stale"] is False
    second = platform.temp.apply_temperature(38.0, clock.now(), rack.pack_ratings())
    assert second["baseline_refreshed"] is True
    assert [item["pack_id"] for item in second["refreshed_packs"]] == ["rk-01-p1"]
    assert platform.temp.baseline("rk-01-p1").epoch == 2
    assert platform.temp.baseline_report(clock.now(), rack.pack_ratings())["packs"]["rk-01-p1"]["reason"] == "fresh"


def test_local_rebaseline_leaves_other_packs_alone(platform, clock):
    rack = platform.racks["rk-01"]
    platform.temp.apply_temperature(30.0, clock.now(), rack.pack_ratings())
    rack.replace_pack_rating(0, 3.35)
    platform.temp.apply_temperature(30.0, clock.now(), rack.pack_ratings())
    # 局部重标：只有换了电芯的那一包绑到新额定值，其余包基线不动
    assert platform.temp.baseline("rk-01-p1").rating_v == 3.35
    assert platform.temp.baseline("rk-01-p1").epoch == 2
    for pack_id in ("rk-01-p2", "rk-01-p3", "rk-01-p8"):
        assert platform.temp.baseline(pack_id).rating_v == 3.2
        assert platform.temp.baseline(pack_id).epoch == 1
    # 混装簇对外口径取最保守值
    assert rack.rating_v() == 3.2


def test_baseline_history_keeps_before_and_after(platform, clock):
    rack = platform.racks["rk-01"]
    platform.temp.apply_temperature(30.0, clock.now(), rack.pack_ratings())
    rack.replace_pack_rating(0, 3.35)
    platform.temp.apply_temperature(30.0, clock.now(), rack.pack_ratings())
    history = platform.temp.baseline("rk-01-p1").history()
    assert len(history) == 2
    first, second = history
    assert first["reason"] == "uninitialised"
    assert first["previous_epoch"] == 0
    assert second["reason"] == "rating_changed"
    assert second["previous_rating_v"] == 3.2
    assert second["rating_v"] == 3.35
    assert second["previous_rating_epoch"] == 0
    assert second["rating_epoch"] == 1
    # 没换的包只有一条标定记录
    assert len(platform.temp.baseline("rk-01-p2").history()) == 1


def test_baseline_expires_by_time(platform, clock):
    rack = platform.racks["rk-01"]
    platform.temp.apply_temperature(30.0, clock.now(), rack.pack_ratings())
    clock.advance(platform.config.temp.baseline_ttl_s + 1.0)
    report = platform.temp.baseline_report(clock.now(), rack.pack_ratings())
    assert report["packs"]["rk-01-p1"]["stale"] is True
    assert report["packs"]["rk-01-p1"]["reason"] == "expired"
    assert platform.temp.charge_limit_ratio() == pytest.approx(platform.temp.ratio())


def test_charge_limit_uses_floor_before_first_calibration(platform):
    baseline = TempBaseline(platform.config.temp.baseline_ttl_s)
    assert baseline.epoch == 0
    assert baseline.stale(0, platform.clock.now()) is True
    assert baseline.latest(platform.clock.now(), 0)["reason"] == "uninitialised"
    with pytest.raises(ValueError):
        baseline.refresh(0.0, 0, 0.0)
    baseline.refresh(3.2, 0, 0.0)
    assert baseline.latest(0.0, 0)["reason"] == "fresh"


def test_charge_limit_is_strictest_across_packs(platform, clock):
    rack = platform.racks["rk-01"]
    # 一包基线都没有时，整簇按下限系数处理
    assert platform.temp.charge_limit_ratio() == platform.config.temp.derate_floor_ratio
    platform.temp.apply_temperature(30.0, clock.now(), rack.pack_ratings())
    assert platform.temp.charge_limit_ratio() == 1.0
    # 登记一个尚未标定的包，整簇立即被它压到下限
    platform.temp.baseline("rk-01-p9")
    detail = platform.temp.charge_limit_detail()
    assert detail["ratio"] == platform.config.temp.derate_floor_ratio
    assert detail["limiting_pack"] == "rk-01-p9"
    assert detail["baseline_epoch"] == 0


def test_charge_limit_detail_names_baseline_generation(platform, clock):
    rack = platform.racks["rk-01"]
    platform.temp.apply_temperature(50.0, clock.now(), rack.pack_ratings())
    rack.replace_pack_rating(0, 3.4)
    platform.temp.apply_temperature(50.0, clock.now(), rack.pack_ratings())
    detail = platform.temp.charge_limit_detail()
    assert detail["ratio"] < 1.0
    assert detail["limiting_pack"] == "rk-01-p1"
    assert detail["baseline_epoch"] == platform.temp.baseline("rk-01-p1").epoch
    assert detail["rating_epoch"] == 1


def test_replace_pack_switches_derate_current_and_cooling_together(platform):
    platform.start()
    platform.sample_temperature(temperature_c=50.0, dt=5.0)
    assert platform.pcs.derate.ratio < 1.0
    assert platform.temp.cooler.is_running() is True
    report = platform.replace_pack_rating(pack_index=0, rating_v=3.4)
    assert report["ok"]
    actions = [step["action"] for step in report["steps"]]
    assert actions == ["replace_pack", "baseline_refresh", "derate_sync"]
    # 基线刷新、降额系数、充电电流上限（PCS 降额）、冷却条件同一切换
    assert report["after"]["pcs_derate_ratio"] == report["after"]["charge_limit_ratio"]
    assert report["after"]["cooling"] is True
    assert platform.pcs.derate.sources["temperature"] == platform.temp.charge_limit_ratio()
    # PCS 侧能直接回答这次用的是哪一代基线
    assert platform.pcs.derate.meta["limiting_pack"] == "rk-01-p1"
    assert platform.pcs.derate.meta["baseline_epoch"] == platform.temp.baseline("rk-01-p1").epoch
    # 刷新前后的记录都在，可回溯当时为什么压着功率
    assert report["before"]["pcs_derate_ratio"] < 1.0
    sync_events = platform.audit.query(component="pcs", action="derate_sync")
    assert sync_events
    payload = sync_events[0]["payload"]
    assert payload["baseline_epoch"] == platform.temp.baseline("rk-01-p1").epoch
    assert payload["previous_ratio"] == report["before"]["pcs_derate_ratio"]


def test_replace_pack_at_normal_temperature_releases_limit(platform):
    platform.start()
    platform.sample_temperature(temperature_c=30.0, dt=5.0)
    report = platform.replace_pack_rating(pack_index=0, rating_v=3.4)
    assert report["ok"]
    # 正常温度本就不该降额：重标后降额、电流上限、冷却都处于放开状态
    assert report["after"]["charge_limit_ratio"] == 1.0
    assert report["after"]["pcs_derate_ratio"] == 1.0
    assert report["after"]["cooling"] is False
    assert platform.pcs.derate.ratio == 1.0
