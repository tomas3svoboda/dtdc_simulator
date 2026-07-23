# GUI release audit

Date: 2026-07-23

## Scope

The audit covers the NiceGUI operator surface, facade callbacks, scenario
initialization, units/bases, actuator grouping, solver-status propagation,
and local application startup.

## Findings corrected

| Finding | Correction | Verification |
|---|---|---|
| One indirect-steam slider per tray exposed an unrealistic operator model | HMI now has only `Predesolv jacket [kg/s total]` and `Toast jacket [kg/s total]` | Source inspection plus facade group tests |
| SPARGE jacket duty was hidden while other per-tray jackets were exposed | Toast zone total includes MAIN and SPARGE jackets | Zone key construction in `tower.py` |
| Feed moisture slider was dry basis while labeled/read as wet basis | Exact wet-to-dry conversion added | Formula uses live X2 and X3 |
| Feed hexane wet-basis conversion omitted oil | Full denominator `1 + X1 + X2 + X3` used for slider and card | Source inspection and live-oil plumbing test |
| Zone-total writes could lose the internal tray distribution | Facade preserves the current split atomically and redistributes after actuator saturation | Unit tests cover normal and saturated allocations |
| Saved GUI seed differed from the release benchmark | Scenario now initializes the COAMO feed and 20/20/20 validation-qualified mesh | Initial-state regression requires `dt_converged` |
| Live feed oil stopped at the facade | Oil now propagates through `OperatingSeed`, `Inputs`, FTRZ, and particle equilibrium | Regression overrides deliberately stale frozen X3 |
| Direct-steam slider permitted unsupported low-flow cases | HMI range is 3-5 kg/s; 3.9 kg/s is the benchmark seed | Off-design sweep established the installed FTRZ-domain floor |

Per-tray indirect-steam MVs remain addressable through the facade/OPC UA
integration. The two HMI zone controls are therefore an operator abstraction,
not a loss of actuator granularity.

## Startup and state checks

- The application starts with `--no-opcua` on `127.0.0.1:8080`.
- HTTP startup probe returns 200 and the server log contains no exception.
- Authoritative assembly starts with a converged DT state: 11 outer
  iterations and benchmark-consistent tray values.
- Feed-card benchmark display basis is approximately 8.13% wet moisture,
  25.43% wet hexane, and 1.37% dry-basis residual oil.
- Jacket-zone seeds correspond to about 1.018 kg/s PREDESOLV condensate and
  0.0885 kg/s TOAST condensate at 2.26 MJ/kg.
- Facade lifecycle, MV/DV routing, grouped arm speed, zone-total jacket
  allocation, saturation redistribution, and live oil are covered by tests.

## Runtime visual inspection limitation

The connected in-app browser bridge could not be initialized in this session:
its tool rejected every call with `codex/sandbox-state-meta: missing field
sandboxPolicy`. The local application itself remained healthy. A separate
headless-browser launch was also blocked by the environment's browser policy,
so no screenshot is claimed as release evidence.

This is the only incomplete part of the GUI audit. Before merging, perform
one visual smoke check in the IDE browser:

1. confirm exactly two jacket controls and no per-tray jacket sliders;
2. confirm COAMO feed values and absence of `SolverStress` at assembly;
3. move each jacket-zone slider and verify its total while tray proportions
   remain fixed;
4. move feed moisture/hexane sliders and confirm their wet-basis readouts;
5. run/pause/reset once and check the narrow and wide layouts for clipping.

## Verdict

The GUI's data/control logic and local server startup pass. Visual layout and
interactive rendering remain conditionally unverified because the supplied
browser-control channel failed outside the application. That condition is a
merge blocker for a strict GUI release, but not evidence of an application
defect.
