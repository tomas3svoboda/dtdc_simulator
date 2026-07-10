# DECISIONS.md

Log of `DECIDE` choices made while building the DTDC simulator, per
`Specifications/DTDC_Simulator_BuildSpec.md`. Newest entries at the top.

## M0 — Walking skeleton (2026-07-10)

**Milestone scope.** Implemented BuildSpec §14 M0: config models + validators,
`RuntimeFacade`, a placeholder `Model.step()`, both clocks, a threaded tick
loop, a full-address-space OPC UA server, and a NiceGUI setup/dashboard UI.
No Coletto (2022) zonal physics yet (M1/M2) — see "Placeholder physics" below.

### UI framework (§10, DECIDE)
Chose **NiceGUI** over Dash. Reasoning given to and confirmed by the user:
pure-Python reactive components, direct in-process calls into
`RuntimeFacade` (no separate callback-graph server), and `app.on_startup`
lets the OPC UA server run as an asyncio task inside NiceGUI's own event
loop — the whole process needs only one asyncio loop plus the tick-loop
worker thread (§8.3), rather than juggling two.

### Runtime port / endpoint (§9, DECIDE)
`opc.tcp://0.0.0.0:4840/dtdc/`, namespace `http://dtdc.sim/`, as suggested
in the spec text. NiceGUI dashboard defaults to `127.0.0.1:8080` (CLI flags
`--host`/`--port`/`--opcua-endpoint`/`--no-opcua` on `dtdc-sim`).

### Tick cadence (§8.1, DECIDE)
`dt_wall_s = 0.2` s (from the example scenario file, kept as-is). Idle-state
poll interval (not in spec, an M0 implementation detail) is 50 ms.

### Placeholder physics (§7 vs §14 M0)
`core/model.py` models each DT/DC stage as a first-order-lag holdup
(`x_new = x_eq + (x_old - x_eq)*exp(-dt/tau)`) chained top-to-bottom, with
`tau` set by sweep-arm speed (DT) or a fixed constant (DC), and per-stage
equilibrium targets driven by steam duty / air conditions. This is
deliberately **not** the Coletto dual-scale zonal model (PHZ/FTRZ/DCZ,
receding front, 12-layer particle FVM) — that is M1/M2 work. The exponential
form was chosen (over explicit Euler) so the placeholder stays numerically
stable at any `dt`, including the large `dt` produced by high `speed_factor`
in `RealTimeClock` — important since M0 already has to survive the full
speed-factor/undersampling machinery in §8.4.

The quality kinetics (§7.11: TIA biexponential blend, protein denaturation)
**are** implemented from the spec's exact Arrhenius parameters, but each is
simplified to a single first-order decay per tick
(`C_TIA(t+dt) = C_TIA(t)*exp(-k_eff(T)*dt)`, `k_eff = A*k1 + (1-A)*k2`)
rather than the literal biexponential-in-elapsed-time form, since the
elapsed-time formulation assumes a batch/plug-flow frame that doesn't map
cleanly onto per-tick recurrence. Revisit if TIA validation against
literature curves requires the exact biexponential shape.

`feed_temperature` (an `OperatingDefaults` field) is treated as a fixed
boundary value captured at `assemble()` time, not a runtime-adjustable
MV/DV — the BuildSpec §5.2 MV/DV tables do not list it as either. If a
future milestone needs it live-adjustable, promote it to a DV.

### MV limits (§5.2, DECIDE — no numeric limits given)
First-cut `(min, max)` ranges in `engine/facade.py::MV_LIMITS`, sized off
the magnitudes in `Specifications/DTDC_default_parameters.yaml`
(`operating_defaults`). All `rate_limit`s are `None` (unlimited slew) for
M0. These are placeholders for real plant limits and should be revisited
once real equipment data is available.

### MV default mode at startup (§6, DECIDE)
Every MV starts in `MANUAL`, seeded from `operating_defaults` (so
`manual_setpoint == auto_setpoint` at t=0 — no bump if an APC connects and
immediately gets switched to AUTO). Rationale: the plant should not be
"driven" by an unconnected APC's stale `auto_setpoint` by default.

### OPC UA write propagation (§9.2, DECIDE)
`asyncua`'s `Node.set_writable()` only gates the *client* write permission;
there is no built-in low-latency "on write" server hook without subclassing
its `AttributeService`. M0 uses a **poll-then-push** refresh cycle
(`REFRESH_S = 0.2` s in `interfaces/opcua/server.py`): read all writable
nodes into the facade, then write the facade's snapshot back to all nodes.
Push-after-pull means a UI-side change and an OPC-UA-client-side change
both converge within one refresh cycle. Acceptable for a soft-real-time
sandbox; a future milestone could add a custom `AttributeService`/`SubHandler`
for immediate reaction if APC latency requirements tighten.

### Logging format (§1.2, DECIDE — "optional run logs")
Not implemented in M0 beyond Python's standard `logging` to stdout
(`logging.basicConfig(level=logging.INFO)` in `main.py`). No file-based run
log yet; add one if/when a milestone needs replay/audit trails.

### Python/dependency versions (§12 wheel caveat)
Verified via `pip install --dry-run` before scaffolding: `numpy` 2.5.1,
`scipy` 1.18.0, and `pydantic-core` 2.46.4 all ship `cp314-win_amd64`
wheels. No fallback to 3.13 was needed.

## Post-M0 polish (2026-07-10)

**Material properties moved to `properties/<material>.yaml`.** §11 states
the intended file layout: "one file per material property set under
properties/; one scenario file binding a property set + model params +
geometry + operating/disturbance defaults." M0 had shipped with `physical:`
embedded directly in `scenarios/soybean_default.yaml` (copied wholesale from
`Specifications/DTDC_default_parameters.yaml`) — functionally fine but not
matching that layout, and it left `properties/` sitting empty. Split the
`physical:` block out into `properties/soybean.yaml`; `config/loader.py`'s
`load_scenario()` now resolves `properties/<material>.yaml` automatically
whenever a scenario omits an inline `physical:` block (an inline block still
overrides the lookup, for one-off scenario tweaks or tests, without needing
a new properties file). `ScenarioConfig` itself is unchanged — only the
loader gained this resolution step — so `config/builder.py` and everything
downstream needed no changes.

**Formatting/lint targets bumped to cp314.** `pyproject.toml`'s
`[tool.ruff] target-version` was left at the scaffold default `"py313"`;
bumped to `"py314"` to match `requires-python = ">=3.14"`, and added an
explicit `[tool.black]` section (also `py314`) since `black` was a declared
dev dependency but had no config and was never actually run over the
codebase during M0. Running it now reformatted 8 files to its 100-column
style; behavior is unchanged (confirmed by the full test suite + `ruff
check` staying green after).

**Added `.gitignore`.** The project has no VCS yet, but `.venv/`,
`__pycache__/`, `*.egg-info/`, and the `pytest`/`ruff` cache dirs should
never be committed once one is initialized.
