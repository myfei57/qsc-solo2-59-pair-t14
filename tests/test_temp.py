"""温控降额与额定值基线。"""

from __future__ import annotations

import pytest


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
    first = platform.temp.apply_temperature(38.0, clock.now(), rack.rating_v(), rack.rating_epoch)
    assert first["baseline_refreshed"] is True
    assert platform.temp.baseline.epoch == 1
    assert platform.temp.baseline.stale(rack.rating_epoch, clock.now()) is False
    rack.replace_pack_rating(0, 3.35)
    report = platform.temp.baseline_report(clock.now(), rack.rating_epoch)
    assert report["stale"] is True
    assert report["reason"] == "rating_changed"
    second = platform.temp.apply_temperature(38.0, clock.now(), rack.rating_v(), rack.rating_epoch)
    assert second["baseline_refreshed"] is True
    assert platform.temp.baseline.epoch == 2
    assert platform.temp.baseline_report(clock.now(), rack.rating_epoch)["reason"] == "fresh"


def test_baseline_expires_by_time(platform, clock):
    rack = platform.racks["rk-01"]
    platform.temp.apply_temperature(30.0, clock.now(), rack.rating_v(), rack.rating_epoch)
    clock.advance(platform.config.temp.baseline_ttl_s + 1.0)
    report = platform.temp.baseline_report(clock.now(), rack.rating_epoch)
    assert report["stale"] is True
    assert report["reason"] == "expired"
    assert platform.temp.charge_limit_ratio() == pytest.approx(platform.temp.ratio())


def test_baseline_refresh_switches_three_limits_together(platform, clock):
    platform.start()
    sampled = platform.sample_temperature(temperature_c=41.0)
    assert sampled["charge_limit_ratio"] < 1.0
    assert platform.pcs.ladder.max_a < platform.config.pcs.current_max_a

    refreshed = platform.replace_pack_rating(pack_index=0, rating_v=3.4, scope="cluster")
    policy = refreshed["thermal_policy"]

    assert policy["scope"] == "cluster"
    assert policy["generation"] == "rating-3.4000v"
    assert policy["charge_limit_ratio"] == 1.0
    assert policy["cooling_start_c"] == 42.5
    assert platform.pcs.thermal_policy()["derate"]["ratio"] == 1.0
    assert platform.pcs.ladder.max_a == 188.24
    assert policy["previous_policy"]["charge_limit_ratio"] < 1.0


def test_mixed_cluster_uses_local_refresh_and_keeps_old_pack_conservative(platform):
    platform.start()
    platform.sample_temperature(temperature_c=41.0)
    refreshed = platform.replace_pack_rating(pack_index=0, rating_v=3.4)
    policy = refreshed["thermal_policy"]

    assert refreshed["steps"][0]["detail"]["scope"] == "local"
    assert policy["selected_refresh_scope"] == "local"
    assert set(policy["generations"]) == {"rating-3.2000v", "rating-3.4000v"}
    assert policy["generation"] == "rating-3.2000v"
    assert policy["governing_pack_id"] != "rk-01-p1"
    assert policy["charge_limit_ratio"] < 1.0
    assert policy["cooling_start_c"] == platform.config.temp.derate_start_c
    with pytest.raises(ValueError):
        platform.replace_pack_rating(pack_index=1, rating_v=3.4, scope="cluster")


def test_thermal_audit_keeps_before_and_after_baselines(platform):
    platform.start()
    platform.sample_temperature(temperature_c=30.0)
    platform.replace_pack_rating(pack_index=0, rating_v=3.4)

    records = platform.audit.query(action="baseline_refresh_committed")
    payload = records[0]["payload"]
    assert payload["before"]["mixed"] is False
    assert payload["after"]["rk-01-p1"]["rating_v"] == 3.4
    assert payload["generation"] == "rating-3.4000v"
    assert payload["switched_together"]["charge_current_max_a"] == 188.24
    assert platform.temp.baseline.history()[-1]["before"]["profile"]["rating_v"] == 3.2
    assert platform.pcs.thermal_history()[-1]["derate"]["ratio"] == 1.0


def test_charge_limit_uses_floor_before_first_calibration(platform):
    baseline = type(platform.temp.baseline)(platform.config.temp.baseline_ttl_s)
    assert baseline.epoch == 0
    assert baseline.stale(0, platform.clock.now()) is True
    assert baseline.latest(platform.clock.now(), 0)["reason"] == "uninitialised"
    with pytest.raises(ValueError):
        baseline.refresh(0.0, 0, 0.0)
    baseline.refresh(3.2, 0, 0.0)
    assert baseline.latest(0.0, 0)["reason"] == "fresh"
