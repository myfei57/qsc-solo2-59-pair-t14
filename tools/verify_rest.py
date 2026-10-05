"""回放其余受改动影响的测试文件（无 pytest 环境下的等效验证）。"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from batterystore.app import build_platform  # noqa: E402
from batterystore.clock import ManualClock  # noqa: E402
from batterystore.config import default_config  # noqa: E402
from batterystore.console.pages import PageLibrary  # noqa: E402
from batterystore.console.server import ConsoleServer, probe_endpoints  # noqa: E402
from batterystore.errors import InterlockError, NamespaceError  # noqa: E402
from batterystore.rack.alarms import collect_alarms  # noqa: E402
from batterystore.rack.mapping import RackMapping  # noqa: E402
from batterystore.rack.metrics import compute_metrics  # noqa: E402
from support import bring_online, connect_grid, prepare_charge  # noqa: E402

PASS = []


def check(name, condition, info=""):
    if not condition:
        raise AssertionError(f"{name} 失败 {info}")
    PASS.append(name)


def new_platform(racks=2):
    clock = ManualClock()
    tmp = tempfile.mkdtemp(prefix="verify2-state-")
    return build_platform(default_config(), clock=clock, storage_root=tmp, rack_count=racks), clock


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def main() -> int:
    platform, clock = new_platform()

    # --- test_rack.py -----------------------------------------------------
    rack = platform.racks["rk-01"]
    metrics = rack.metrics()
    check("rack.metrics_zero", approx(metrics.delta_mv, 0.0))
    check("rack.cells", metrics.cells == platform.config.rack.cell_count)
    check("rack.energy", rack.energy_kwh(platform.config.soc.capacity_ah) > 0)
    cells = rack.cells()
    cells[0].update(voltage_v=cells[0].voltage_v + 0.05, temperature_c=48.0)
    spread = compute_metrics(cells)
    check("rack.spread", approx(spread.delta_mv, 50.0, 0.5) and spread.max_temperature_c == 48.0)
    before = rack.rating_epoch
    change = rack.replace_pack_rating(0, 3.35)
    check("rack.epoch_bumps", change["rating_epoch"] == before + 1 and rack.rating_epoch == before + 1)
    check("rack.change_fields", change["rating_v"] == 3.35 and change["pack_rating_epoch"] == 1)
    check("rack.rating_v", rack.rating_v() in (3.2, 3.35))
    rack.apply_temperature(platform.config.rack.temp_trip_c)
    alarms = collect_alarms(rack.rack_id, rack.metrics(), platform.config.rack, rack.soc_pct(), 60.0)
    check("rack.alarm", "over_temp_trip" in {a.code for a in alarms})
    mapping = RackMapping()
    mapping.assign("rk-01", "R1", "Z1")
    mapping.assign("rk-02", "R2", "Z2")
    check("mapping.reassign", mapping.assign("rk-01", "R9", "Z2")["previous_label"] == "R1")
    try:
        mapping.label_of("rk-99")
        check("mapping.unknown", False)
    except NamespaceError:
        check("mapping.unknown", True)

    # --- test_pcs.py ------------------------------------------------------
    platform2, _ = new_platform()
    factor = platform2.pcs.apply_derate(temperature=0.5, grid=0.8)
    check("pcs.strictest", factor.ratio == 0.5 and factor.as_dict()["sources"]["grid"] == 0.8)
    point = platform2.pcs.set_point(300.0, 1.0)
    check("pcs.setpoint_derate", point["derate"]["ratio"] == 0.5)
    ladder = platform2.pcs.ladder
    ladder.begin(120.0)
    ladder.lower_to(40.0)
    try:
        ladder.raise_to(80.0)
        check("pcs.ladder_interlock", False)
    except InterlockError:
        check("pcs.ladder_interlock", True)
    check("pcs.ladder_verify", ladder.verify_voltage(3.2, 3.2, 0.05) is True)
    check("pcs.ladder_raise", ladder.raise_to(80.0) == 80.0)

    # --- test_sequencing.py -------------------------------------------------
    platform3, _ = new_platform()
    bring_online(platform3)
    report = connect_grid(platform3)
    actions = [step["action"] for step in report["steps"]]
    check("seq.tie_order", actions.index("ready_confirmed") < actions.index("transfer") < actions.index("tie_check") < actions.index("clamp"))
    prepare_charge(platform3)
    charge = platform3.charge(steps=3, dt=600.0)
    check("seq.charge_ok", charge["ok"], charge["violations"])
    charge_actions = [step["action"] for step in charge["steps"]]
    check("seq.charge_order", charge_actions.index("charge_plan") < charge_actions.index("derate") < charge_actions.index("verify_voltage"))
    discharge = platform3.discharge(power_kw=300.0, dt=600.0)
    check("seq.discharge_ok", discharge["ok"], discharge["violations"])
    platform3.transfer_off_grid()
    check("seq.off_grid", platform3.pcs.state_machine.state == "off_grid")

    platform4, _ = new_platform()
    bring_online(platform4)
    sampled = platform4.sample_temperature(temperature_c=50.0, dt=5.0)
    check("seq.temp_steps", [s["action"] for s in sampled["steps"]] == ["temperature", "derate", "cool"])
    check("seq.temp_ratio", sampled["charge_limit_ratio"] < 1.0)
    replaced = platform4.replace_pack_rating(pack_index=0, rating_v=3.4)
    check("seq.replace_ok", replaced["ok"])
    check("seq.replace_steps", [s["action"] for s in replaced["steps"]] == ["replace_pack", "baseline_refresh", "derate_sync"])
    check("seq.local_p1", platform4.temp.baseline("rk-01-p1").rating_v == 3.4)
    check("seq.local_p2", platform4.temp.baseline("rk-01-p2").rating_v == 3.2)
    cycle = platform4.cycle()
    check("seq.cycle", cycle["cycle"] == 1)
    probe = platform4.concurrency_probe(rounds=12)
    check("seq.probe", probe["ok"] and probe["illegal_states"] == [])
    fault_probe = platform4.concurrency_fault_probe(rounds=6)
    check("seq.fault_probe", fault_probe["ok"] and set(fault_probe["latched"]) == {"rk-01", "rk-02"})
    platform4.sample_temperature(temperature_c=30.0)
    status = platform4.status()
    check("seq.status", status["station"] == "station1" and status["ids_issued"] > 0)
    blocked = platform4.readiness()
    check("seq.blocked", blocked["all_ready"] is False)
    platform4.clear_fault("rk-01")
    platform4.clear_fault("rk-02")
    check("seq.ready", platform4.readiness()["all_ready"] is True)

    # --- test_runtime.py ----------------------------------------------------
    from batterystore.cli import build_parser, main as cli_main

    parser = build_parser()
    names = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001
    check("cli.subcommands", {"serve", "demo", "status", "config", "alerts", "reset"} <= names)
    storage = tempfile.mkdtemp(prefix="verify2-cli-")
    check("cli.config", cli_main(["--storage-root", storage, "config"]) == 0)
    check("cli.status", cli_main(["--storage-root", storage, "status"]) == 0)
    check("cli.alerts", cli_main(["--storage-root", storage, "alerts"]) == 0)
    check("cli.reset", cli_main(["--storage-root", storage, "reset"]) == 0)
    storage2 = tempfile.mkdtemp(prefix="verify2-rebuild-")
    first = build_platform(default_config(), clock=ManualClock(), storage_root=storage2, rack_count=1)
    first.start()
    first.cycle()
    check("runtime.persist", first.store.exists("platform_state"))
    second = build_platform(default_config(), clock=ManualClock(), storage_root=storage2, rack_count=1)
    check("runtime.rebuild", second.status()["cycles"] >= 1)

    # --- test_console_api.py ------------------------------------------------
    library = PageLibrary("web")
    check("pages.resolve", library.resolve("/racks") == "racks" and library.resolve("/api/status") is None)
    check("pages.count", len(library.index()) == 4)
    platform5, _ = new_platform()
    bring_online(platform5)
    server = ConsoleServer(platform5, port=0)
    base_url = server.start()
    try:
        with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as resp:
            check("console.status", json.loads(resp.read())["station"] == "station1")
        for path in ("/api/racks", "/api/pcs", "/api/grid", "/api/quota", "/api/soc", "/api/alerts", "/api/temp"):
            with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as resp:
                check(f"console.{path}", resp.status == 200)
        with urllib.request.urlopen(f"{base_url}/api/temp", timeout=5) as resp:
            temp_payload = json.loads(resp.read())
        check("console.temp_packs", "rk-01-p1" in temp_payload["baseline"]["packs"])
        request = urllib.request.Request(
            f"{base_url}/api/replace-pack",
            data=json.dumps({"pack_index": 0, "rating_v": 3.4}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            replaced = json.loads(resp.read())
        check("console.replace_ok", replaced["ok"] is True)
        check("console.replace_steps", [s["action"] for s in replaced["steps"]] == ["replace_pack", "baseline_refresh", "derate_sync"])
        try:
            urllib.request.urlopen(f"{base_url}/api/not-a-route", timeout=5)
            check("console.404", False)
        except urllib.error.HTTPError as error:
            check("console.404", error.code == 404)
        probe = probe_endpoints(base_url, ["/api/status", "/api/audit"])
        check("console.probe", probe["/api/status"]["ok"] and probe["/api/audit"]["ok"])
    finally:
        server.stop()

    print(f"全部通过：{len(PASS)} 项检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
