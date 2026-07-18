"""OPC UA server adapter mirroring a plant DCS (BuildSpec §9).

Security is explicitly disabled (`SecurityPolicy#None`, anonymous) per §9 —
sandbox/edge use only, no encryption/auth. This adapter talks to the plant
only through `RuntimeFacade`; it never imports `core/` (BuildSpec §3).

Node writes from OPC UA clients (the APC) are picked up by polling the
writable nodes once per `REFRESH_S` and pushing changes into the facade,
then pushing the facade's snapshot back out to all nodes. Because push
follows pull every cycle, a UI-originated change (made directly on the
facade) is reflected on the OPC UA side within one refresh cycle, and an
OPC UA client write is applied to the facade within one refresh cycle too
— consistent with the soft-real-time nature of this simulator (BuildSpec
§9.2). `asyncua` has no lower-latency "on write" hook without subclassing
its AttributeService; that hardening is left for a later milestone.
"""

from __future__ import annotations

import asyncio
import logging

from asyncua import Server, ua
from asyncua.common.node import Node

from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.mv import Mode

logger = logging.getLogger(__name__)

ENDPOINT = "opc.tcp://0.0.0.0:4840/dtdc/"
NAMESPACE_URI = "http://dtdc.sim/"
REFRESH_S = 0.2

_KPI_FIELDS = (
    "residual_hexane",
    "meal_moisture",
    "steam_consumption",
    "throughput",
)


class OpcUaAdapter:
    def __init__(self, facade: RuntimeFacade, endpoint: str = ENDPOINT) -> None:
        self._facade = facade
        self._endpoint = endpoint
        self._server: Server | None = None
        self._idx = 0
        self._mv_nodes: dict[str, dict[str, Node]] = {}
        self._dv_nodes: dict[str, Node] = {}
        self._pv_stage_nodes: dict[str, dict[str, Node]] = {}
        self._pv_kpi_nodes: dict[str, Node] = {}
        self._sim_nodes: dict[str, Node] = {}
        self._last_global_mode = Mode.MANUAL.value
        self._last_speed_factor: float | None = None
        self._last_dt_resolve_interval_s: float | None = None
        self._last_mv_mode: dict[str, str] = {}
        self._last_mv_manual: dict[str, float] = {}
        self._last_mv_auto: dict[str, float] = {}
        self._last_dv: dict[str, float] = {}

    async def build(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self._endpoint)
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self._idx = await server.register_namespace(NAMESPACE_URI)
        self._server = server

        dtdc = await server.nodes.objects.add_object(self._idx, "DTDC")
        await self._build_sim_folder(dtdc)
        await self._build_mv_folder(dtdc)
        await self._build_dv_folder(dtdc)
        await self._build_pv_folder(dtdc)

    async def _build_sim_folder(self, parent: Node) -> None:
        idx = self._idx
        sim = await parent.add_object(idx, "Sim")
        snap = self._facade.get_snapshot()

        speed = await sim.add_variable(idx, "SpeedFactor", float(snap.speed_factor))
        await speed.set_writable()
        self._last_speed_factor = float(snap.speed_factor)
        # M3a follow-up ("C"): live-tunable DT resolve cadence, same
        # writable-node/change-detection pattern as SpeedFactor.
        dt_resolve_interval = await sim.add_variable(
            idx, "DTResolveIntervalS", float(snap.dt_resolve_interval_s)
        )
        await dt_resolve_interval.set_writable()
        self._last_dt_resolve_interval_s = float(snap.dt_resolve_interval_s)
        sim_time = await sim.add_variable(idx, "SimTime", float(snap.sim_time))
        actual_speed = await sim.add_variable(idx, "ActualSpeed", float(snap.actual_speed))
        state = await sim.add_variable(idx, "State", snap.state.value)
        global_mode = await sim.add_variable(idx, "GlobalMode", Mode.MANUAL.value)
        await global_mode.set_writable()
        undersample = await sim.add_variable(
            idx, "UndersampleWarning", bool(snap.undersample_warning)
        )
        solver_stress = await sim.add_variable(idx, "SolverStress", bool(snap.solver_stress))
        # M3a: raw convergence diagnostic behind SolverStress's bool summary (§7.9/§9.1).
        dt_outer_iters = await sim.add_variable(
            idx,
            "DTSolverOuterIterations",
            int(snap.outputs.dt_solver_outer_iterations) if snap.outputs else 0,
        )

        self._sim_nodes = {
            "SpeedFactor": speed,
            "DTResolveIntervalS": dt_resolve_interval,
            "SimTime": sim_time,
            "ActualSpeed": actual_speed,
            "State": state,
            "GlobalMode": global_mode,
            "UndersampleWarning": undersample,
            "SolverStress": solver_stress,
            "DTSolverOuterIterations": dt_outer_iters,
        }

        facade = self._facade

        async def do_run(parent_node: Node) -> list:
            facade.run()
            return []

        async def do_pause(parent_node: Node) -> list:
            facade.pause()
            return []

        async def do_stop(parent_node: Node) -> list:
            facade.stop()
            return []

        async def do_reset(parent_node: Node) -> list:
            facade.reset()
            return []

        async def do_reconfigure(parent_node: Node) -> list:
            facade.reconfigure()
            return []

        await sim.add_method(idx, "Run", do_run, [], [])
        await sim.add_method(idx, "Pause", do_pause, [], [])
        await sim.add_method(idx, "Stop", do_stop, [], [])
        await sim.add_method(idx, "Reset", do_reset, [], [])
        await sim.add_method(idx, "Reconfigure", do_reconfigure, [], [])

    async def _build_mv_folder(self, parent: Node) -> None:
        idx = self._idx
        mv_root = await parent.add_object(idx, "MV")
        snap = self._facade.get_snapshot()
        group_nodes: dict[str, Node] = {}

        for key, mv in snap.mvs.items():
            mv_key, _, stage_id = key.partition("/")

            if mv_key not in group_nodes:
                group_nodes[mv_key] = await mv_root.add_object(idx, mv_key)
            node_parent = group_nodes[mv_key]
            if stage_id:
                node_parent = await node_parent.add_object(idx, stage_id)

            mode_n = await node_parent.add_variable(idx, "Mode", mv.mode.value)
            await mode_n.set_writable()
            manual_n = await node_parent.add_variable(
                idx, "ManualSetpoint", float(mv.manual_setpoint)
            )
            await manual_n.set_writable()
            auto_n = await node_parent.add_variable(idx, "AutoSetpoint", float(mv.auto_setpoint))
            await auto_n.set_writable()
            eff_n = await node_parent.add_variable(idx, "EffectiveValue", float(mv.effective_value))
            min_n = await node_parent.add_variable(idx, "Min", float(mv.min))
            max_n = await node_parent.add_variable(idx, "Max", float(mv.max))

            self._mv_nodes[key] = {
                "Mode": mode_n,
                "ManualSetpoint": manual_n,
                "AutoSetpoint": auto_n,
                "EffectiveValue": eff_n,
                "Min": min_n,
                "Max": max_n,
            }
            self._last_mv_mode[key] = mv.mode.value
            self._last_mv_manual[key] = float(mv.manual_setpoint)
            self._last_mv_auto[key] = float(mv.auto_setpoint)

    async def _build_dv_folder(self, parent: Node) -> None:
        idx = self._idx
        dv_root = await parent.add_object(idx, "DV")
        snap = self._facade.get_snapshot()
        for key, value in snap.dvs.items():
            n = await dv_root.add_variable(idx, key, float(value))
            await n.set_writable()
            self._dv_nodes[key] = n
            self._last_dv[key] = float(value)

    async def _build_pv_folder(self, parent: Node) -> None:
        idx = self._idx
        pv_root = await parent.add_object(idx, "PV")
        stage_root = await pv_root.add_object(idx, "Stage")
        kpi_root = await pv_root.add_object(idx, "KPI")

        outputs = self._facade.get_snapshot().outputs
        if outputs is None:
            return

        for stage_id in outputs.stage_T:
            stage_obj = await stage_root.add_object(idx, stage_id)
            fields = {
                "T": outputs.stage_T[stage_id],
                "X_hex": outputs.stage_X_hex_ppm[stage_id],
                "X_w": outputs.stage_X_w_pct[stage_id],
                "VaporTemp": outputs.stage_vapor_temp[stage_id],
                "Level": outputs.stage_level_pct[stage_id],
            }
            node_map: dict[str, Node] = {}
            for fname, fval in fields.items():
                node_map[fname] = await stage_obj.add_variable(idx, fname, float(fval))
            self._pv_stage_nodes[stage_id] = node_map

        kpi_map = {
            "residual_hexane": outputs.kpi_residual_hexane_ppm,
            "meal_moisture": outputs.kpi_meal_moisture_pct,
            "steam_consumption": outputs.kpi_steam_consumption_kg_per_t,
            "throughput": outputs.kpi_throughput_t_per_day,
        }
        for fname in _KPI_FIELDS:
            self._pv_kpi_nodes[fname] = await kpi_root.add_variable(
                idx, fname, float(kpi_map[fname])
            )

    async def _pull_writes(self) -> None:
        """Apply a node's value to the facade only if it actually changed since
        our own last `_push_snapshot` wrote it. Nodes are re-written every
        cycle regardless of origin, so without this guard a value the UI (or
        anything else) set directly on the facade would be read back here as
        "unchanged from the node" and instantly stomped by the node's stale
        copy — see module docstring's push-follows-pull assumption."""
        facade = self._facade

        speed_val = float(await self._sim_nodes["SpeedFactor"].read_value())
        if speed_val != self._last_speed_factor:
            facade.set_speed_factor(speed_val)
            self._last_speed_factor = speed_val

        dt_resolve_val = float(await self._sim_nodes["DTResolveIntervalS"].read_value())
        if dt_resolve_val != self._last_dt_resolve_interval_s:
            facade.set_dt_resolve_interval_s(dt_resolve_val)
            self._last_dt_resolve_interval_s = dt_resolve_val

        global_mode_val = await self._sim_nodes["GlobalMode"].read_value()
        if global_mode_val != self._last_global_mode:
            try:
                facade.set_global_mode(Mode(global_mode_val))
            except ValueError:
                logger.warning("Sim/GlobalMode: invalid mode %r", global_mode_val)
            self._last_global_mode = global_mode_val

        for key, nodes in self._mv_nodes.items():
            mode_val = await nodes["Mode"].read_value()
            if mode_val != self._last_mv_mode[key]:
                try:
                    facade.set_mv_mode(key, Mode(mode_val))
                except ValueError:
                    logger.warning("MV/%s/Mode: invalid mode %r", key, mode_val)
                self._last_mv_mode[key] = mode_val

            manual_val = float(await nodes["ManualSetpoint"].read_value())
            if manual_val != self._last_mv_manual[key]:
                facade.set_mv_manual_setpoint(key, manual_val)
                self._last_mv_manual[key] = manual_val

            auto_val = float(await nodes["AutoSetpoint"].read_value())
            if auto_val != self._last_mv_auto[key]:
                facade.set_mv_auto_setpoint(key, auto_val)
                self._last_mv_auto[key] = auto_val

        for key, node in self._dv_nodes.items():
            val = float(await node.read_value())
            if val != self._last_dv[key]:
                facade.set_dv(key, val)
                self._last_dv[key] = val

    async def _push_snapshot(self) -> None:
        snap = self._facade.get_snapshot()

        await self._sim_nodes["SimTime"].write_value(float(snap.sim_time))
        await self._sim_nodes["ActualSpeed"].write_value(float(snap.actual_speed))
        await self._sim_nodes["State"].write_value(snap.state.value)
        await self._sim_nodes["SpeedFactor"].write_value(float(snap.speed_factor))
        await self._sim_nodes["DTResolveIntervalS"].write_value(float(snap.dt_resolve_interval_s))
        await self._sim_nodes["UndersampleWarning"].write_value(bool(snap.undersample_warning))
        await self._sim_nodes["SolverStress"].write_value(bool(snap.solver_stress))
        if snap.outputs is not None:
            await self._sim_nodes["DTSolverOuterIterations"].write_value(
                int(snap.outputs.dt_solver_outer_iterations)
            )

        for key, mv in snap.mvs.items():
            nodes = self._mv_nodes[key]
            await nodes["Mode"].write_value(mv.mode.value)
            await nodes["ManualSetpoint"].write_value(float(mv.manual_setpoint))
            await nodes["AutoSetpoint"].write_value(float(mv.auto_setpoint))
            await nodes["EffectiveValue"].write_value(float(mv.effective_value))

        for key, value in snap.dvs.items():
            await self._dv_nodes[key].write_value(float(value))

        outputs = snap.outputs
        if outputs is None:
            return
        for stage_id, nodes in self._pv_stage_nodes.items():
            await nodes["T"].write_value(float(outputs.stage_T[stage_id]))
            await nodes["X_hex"].write_value(float(outputs.stage_X_hex_ppm[stage_id]))
            await nodes["X_w"].write_value(float(outputs.stage_X_w_pct[stage_id]))
            await nodes["VaporTemp"].write_value(float(outputs.stage_vapor_temp[stage_id]))
            await nodes["Level"].write_value(float(outputs.stage_level_pct[stage_id]))

        kpi_map = {
            "residual_hexane": outputs.kpi_residual_hexane_ppm,
            "meal_moisture": outputs.kpi_meal_moisture_pct,
            "steam_consumption": outputs.kpi_steam_consumption_kg_per_t,
            "throughput": outputs.kpi_throughput_t_per_day,
        }
        for fname in _KPI_FIELDS:
            await self._pv_kpi_nodes[fname].write_value(float(kpi_map[fname]))

    async def run(self) -> None:
        assert self._server is not None
        async with self._server:
            while not self._facade.is_shutdown():
                try:
                    await self._pull_writes()
                    await self._push_snapshot()
                except Exception:
                    logger.exception("OPC UA refresh cycle failed")
                await asyncio.sleep(REFRESH_S)


async def serve(facade: RuntimeFacade, endpoint: str = ENDPOINT) -> None:
    """Build the address space from the (already-assembled) facade and serve forever."""
    adapter = OpcUaAdapter(facade, endpoint)
    await adapter.build()
    logger.info("OPC UA server listening on %s (SecurityPolicy#None, anonymous)", endpoint)
    await adapter.run()
