"""Industrial calibration scorecard for the DTDC model.

Scores the model's steady-state DT and DC outputs against the defensible
industrial targets compiled in
`literature_sources/Svoboda_Industrial_DTDC_Model_Calibration_Targets.pdf`
(COAMO plant data from Paraiso et al. 2008 + the Kemper 2019 AOCS envelope).

Per that report's own recommendation, the DT and DC are scored SEPARATELY:
the DT owns essentially all safety-critical hexane removal; the DC is a
moisture/temperature finisher whose hexane coefficient is a weak polishing
term, not identifiable from a final-meal measurement alone.

This is the objective function for the convergence/recalibration work (Phase 1
of that plan): it exposes the gaps at the true COAMO feed COMPOSITION, and it
is the regression guard so the model can never again silently drift off-target
behind a non-converged solver. Run:

    python scripts/calibration_scorecard.py [scenario.yaml]

Intensive targets (temperatures, mass fractions, ppm, specific steam) are
scale-independent and scored directly. Throughput/residence context is printed
alongside so scale-dependent metrics (residual hexane, moisture) can be judged
fairly -- the model geometry here is the scenario's, not COAMO's, so residence
time is reported, not assumed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running as a plain script (python scripts/calibration_scorecard.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[0].parent / "src"))

from dtdc_simulator.config.loader import load_scenario  # noqa: E402
from dtdc_simulator.config.builder import assemble_model  # noqa: E402
from dtdc_simulator.core import dt_solver  # noqa: E402
from dtdc_simulator.core.model import (  # noqa: E402
    DC_ROLES,
    DT_ROLES,
    Inputs,
    StageRole,
    _build_dt_trays,
    _dt_role_stages,
    _shaft_rpm,
)

# --- COAMO feed, dry-defatted-meal basis (report Table 1 "Model-state conversion") ---
COAMO_FEED = {
    "T_C": 49.0,
    "X_hexane": 0.388,  # kg hexane / kg dry defatted meal
    "X_water": 0.124,  # kg water  / kg dry defatted meal
    "X_oil": 0.0137,  # kg oil    / kg dry defatted meal
}
# COAMO stream basis: 40 t/h raw soybean -> 28.32 t/h dry defatted meal.
DRY_MEAL_PER_RAW_SOY = 28.32 / 40.0


@dataclass
class Metric:
    name: str
    value: float
    unit: str
    low: float
    high: float
    central: float | None = None
    note: str = ""

    @property
    def status(self) -> str:
        if self.low <= self.value <= self.high:
            return "PASS"
        # WARN if within 25% of the band width outside it, else FAIL.
        width = max(self.high - self.low, abs(self.high) * 0.1, 1e-9)
        off = min(abs(self.value - self.low), abs(self.value - self.high))
        return "WARN" if off <= 0.25 * width else "FAIL"

    def render(self) -> str:
        band = f"[{self.low:g}, {self.high:g}]"
        if self.central is not None:
            band += f" c={self.central:g}"
        tag = {"PASS": "PASS", "WARN": "warn", "FAIL": "FAIL"}[self.status]
        return f"  [{tag}] {self.name:<34} {self.value:9.3f} {self.unit:<10} target {band:<22} {self.note}"


def _wb_moisture_pct(X1: float, X2: float, X3: float) -> float:
    """Wet-basis water mass fraction (%), the industrial reporting convention:
    water / (dry defatted meal + water + hexane + oil). Matches the report's
    own conversion (0.190/... = 19 wt% <-> X1=0.238 kg/kg)."""
    return 100.0 * X1 / (1.0 + X1 + X2 + X3)


def _build_inputs(cfg, coamo_feed: bool) -> Inputs:
    od = cfg.operating_defaults
    dd = cfg.disturbance_defaults
    if coamo_feed:
        feed_T = COAMO_FEED["T_C"] + 273.15
        feed_X_water = COAMO_FEED["X_water"]
        feed_X_hex = COAMO_FEED["X_hexane"]
        feed_X_oil = COAMO_FEED["X_oil"]
    else:
        feed_T = dd.feed_temperature
        feed_X_water = dd.feed_moisture
        feed_X_hex = dd.feed_hexane
        feed_X_oil = getattr(dd, "feed_oil", 0.01)
    return Inputs(
        feed_flow_rate=od.feed_flow_rate,
        feed_temperature=feed_T,
        indirect_steam=dict(od.indirect_steam),
        direct_steam=dict(od.direct_steam),
        sweep_arm_speed=dict(od.sweep_arm_speed),
        gate_opening=dict(od.gate_opening),
        heated_air_temp=od.heated_air_temp,
        heated_air_flow=od.heated_air_flow,
        ambient_air_temp=dd.ambient_air_temp,
        ambient_air_flow=od.ambient_air_flow,
        feed_moisture=feed_X_water,
        feed_hexane=feed_X_hex,
        feed_oil=feed_X_oil,
        ambient_relative_humidity=dd.ambient_relative_humidity,
    )


def score_dt(model, cfg, u: Inputs) -> tuple[list[Metric], object]:
    c = model.constants
    dt_stages = _dt_role_stages(model.stages)
    trays = _build_dt_trays(dt_stages, u.indirect_steam, u.direct_steam)
    solid_feed = dt_solver.SolidFeed(
        T=u.feed_temperature, X1=u.feed_moisture, X2=u.feed_hexane, X3=u.feed_oil,
        m_dry_kg_s=max(u.feed_flow_rate, 1e-9),
    )
    vapor_feed = dt_solver.VaporFeed(
        m_water_kg_s=c.dt_vapor_feed_water_kg_s, m_hex_kg_s=c.dt_vapor_feed_hex_kg_s,
        T=c.dt_vapor_feed_T,
    )
    r = dt_solver.solve_dt(
        trays, solid_feed, vapor_feed, c.dt_constants,
        nz_phz=c.dt_nz_phz, nz_ftrz=c.dt_nz_ftrz, nz_dcz=c.dt_nz_dcz,
        outer_tol=c.dt_outer_tol, outer_max_iter=c.dt_outer_max_iter,
        dcz_inner_max_iter=c.dt_dcz_inner_max_iter,
        sweep_arm_rpm=_shaft_rpm(model.stages, u.sweep_arm_speed),
    )
    ap = r.axial_profile
    dome_T = ap.vapor_T[0] - 273.15
    dome_hex = ap.vapor_hexane_frac[0] * 100.0
    meal = r.tray_summaries[-1]
    predesolv = r.tray_summaries[0]

    # specific direct steam, kg per tonne raw soybean
    direct_steam_kg_s = sum(u.direct_steam.values())
    raw_soy_kg_s = max(u.feed_flow_rate, 1e-9) / DRY_MEAL_PER_RAW_SOY
    steam_kg_per_t = direct_steam_kg_s / raw_soy_kg_s * 1000.0

    metrics = [
        Metric("DT dome vapor temperature", dome_T, "C", 70.0, 75.0),
        Metric("DT dome vapor hexane", dome_hex, "wt%", 88.0, 94.0, 91.0),
        Metric("DT meal outlet temperature", meal.T - 273.15, "C", 108.0, 112.0, 110.0),
        Metric("DT meal outlet moisture", _wb_moisture_pct(meal.X1, meal.X2, u.feed_oil),
               "wt%wb", 16.0, 21.0, 19.0),
        Metric("DT meal residual hexane", meal.X2 * 1e6, "ppm", 100.0, 500.0, 280.0),
        Metric("DT pre-desolv tray outlet T", predesolv.T - 273.15, "C", 64.0, 72.0, 68.0),
        Metric("DT specific direct steam", steam_kg_per_t, "kg/t_raw", 110.0, 116.0,
               note="(scenario setpoint, not yet calibrated)"),
        # Rigor gate: the OUTER FTRZ/DCZ loop may report converged while the
        # INNER DCZ primary loop silently hits its cap (the core rigor issue).
        # Only a PASS if BOTH are true.
        Metric("DT solver fully converged",
               1.0 if (r.converged and r.dcz.iterations < c.dt_dcz_inner_max_iter) else 0.0,
               "bool", 1.0, 1.0,
               note=f"outer={r.outer_iterations} ({'ok' if r.converged else 'CAP'}), "
                    f"dcz_inner={r.dcz.iterations}/{c.dt_dcz_inner_max_iter} "
                    f"({'ok' if r.dcz.iterations < c.dt_dcz_inner_max_iter else 'CAP'})"),
    ]
    return metrics, r


def score_dc(model, u: Inputs, dt_meal) -> list[Metric]:
    """Chain the DT meal outlet through the DC stages (dryer, then cooler)
    using the model's own steady-state air-contact equilibrium."""
    T_in, X1_in, X2_in = dt_meal.T, dt_meal.X1, dt_meal.X2
    dryer_out = cooler_out = None
    for stage in model.stages:
        if stage.role in DC_ROLES:
            tau = model._stage_tau(stage, u)
            T_eq, X1_eq, X2_eq, *_ = model._dc_equilibrium(stage, T_in, X1_in, X2_in, u, tau)
            if stage.role is StageRole.DRYER:
                dryer_out = (T_eq, X1_eq, X2_eq)
            elif stage.role is StageRole.COOLER:
                cooler_out = (T_eq, X1_eq, X2_eq)
            T_in, X1_in, X2_in = T_eq, X1_eq, X2_eq
    metrics: list[Metric] = []
    if dryer_out is not None:
        metrics.append(Metric("DC dryer outlet moisture",
                              _wb_moisture_pct(dryer_out[1], dryer_out[2], u.feed_oil),
                              "wt%wb", 12.0, 13.0))
        metrics.append(Metric("DC dryer outlet temperature", dryer_out[0] - 273.15, "C", 55.0, 65.0, 60.0))
    if cooler_out is not None:
        delta_ambient = (cooler_out[0] - u.ambient_air_temp)
        metrics.append(Metric("DC cooled meal above ambient", delta_ambient, "K", 0.0, 10.0))
    return metrics


def _residence_context(model, u: Inputs) -> str:
    lines = []
    total_dt = 0.0
    for stage in model.stages:
        tau = model._stage_tau(stage, u)
        role = stage.role.value
        if stage.role in DT_ROLES:
            total_dt += tau
        lines.append(f"    {stage.id:<8} {role:<10} tau={tau/60:5.1f} min  bed={stage.bed_height_m:.2f} m")
    raw_soy_th = u.feed_flow_rate / DRY_MEAL_PER_RAW_SOY * 3.6
    lines.append(f"    DT total solid residence ~ {total_dt/60:.1f} min (Crown patent: >=20 min at 105-110 C)")
    lines.append(f"    feed dry meal = {u.feed_flow_rate:.2f} kg/s  (~{raw_soy_th:.0f} t/h raw soybean basis)")
    return "\n".join(lines)


def main() -> None:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "scenarios/soybean_default.yaml"
    cfg = load_scenario(scenario, properties_dir="properties")
    model, _ = assemble_model(cfg)

    print("=" * 78)
    print(f" DTDC CALIBRATION SCORECARD  --  {scenario}")
    print(" targets: Svoboda_Industrial_DTDC_Model_Calibration_Targets (COAMO + Kemper)")
    print("=" * 78)

    u = _build_inputs(cfg, coamo_feed=True)
    print(f"\n FEED (COAMO, dry-defatted-meal basis): {COAMO_FEED['T_C']:.0f} C  "
          f"X_hex={COAMO_FEED['X_hexane']:.3f}  X_water={COAMO_FEED['X_water']:.3f}  "
          f"X_oil={COAMO_FEED['X_oil']:.4f}")
    print("\n Residence / throughput context:")
    print(_residence_context(model, u))

    dt_metrics, r = score_dt(model, cfg, u)
    print("\n --- DESOLVENTIZER-TOASTER (DT) ---")
    for m in dt_metrics:
        print(m.render())

    print("\n DT tray exit profile (top -> bottom):")
    for s in r.tray_summaries:
        wb = _wb_moisture_pct(s.X1, s.X2, u.feed_oil)
        print(f"    {s.id:<8} T={s.T-273.15:6.1f} C   moisture={wb:5.1f} wt%wb   hexane={s.X2*1e6:9.1f} ppm")

    dc_metrics = score_dc(model, u, r.tray_summaries[-1])
    print("\n --- DRYER-COOLER (DC) ---")
    for m in dc_metrics:
        print(m.render())

    all_metrics = dt_metrics + dc_metrics
    n_pass = sum(1 for m in all_metrics if m.status == "PASS")
    n_warn = sum(1 for m in all_metrics if m.status == "WARN")
    n_fail = sum(1 for m in all_metrics if m.status == "FAIL")
    print("\n" + "=" * 78)
    print(f" SUMMARY: {n_pass} PASS   {n_warn} warn   {n_fail} FAIL   (of {len(all_metrics)})")
    print("=" * 78)


if __name__ == "__main__":
    main()
