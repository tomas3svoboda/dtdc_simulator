"""OPC UA server adapter mirroring a plant DCS (BuildSpec §9).

Phase 1 (strict interface): the address space is the *equipment envelope*
(``config/envelope.py``), NOT the loaded scenario. Every canonical stage,
actuator, KPI and control loop for the maximal realistic DTDC is created ONCE at
a fixed path. A given build only marks each node **active** (bound to a live
model quantity, Good quality) or **placeholder** (present, value nulled,
``StatusCode = Bad_NotConnected``, sibling ``Present = false``). A client's tag
map is therefore stable across reconfiguration — reconfiguring only flips the
active mask, it never adds or removes nodes. ``Config/BuildManifest`` publishes
the active mask so a client can discover which nodes are live for the build.

Security is explicitly disabled (`SecurityPolicy#None`, anonymous) per §9 —
sandbox/edge use only. Runtime security/cert control is a later milestone.

This adapter talks to the plant only through `RuntimeFacade`; it never imports
`core/` (BuildSpec §3). Node writes from OPC UA clients (the APC) are polled
once per `REFRESH_S` and pushed into the facade, then the facade's snapshot is
pushed back out — push follows pull every cycle (BuildSpec §9.2), so a
UI-originated change and an OPC UA client write each converge within one cycle.
"""

from __future__ import annotations

import asyncio
import logging

from asyncua import Server, ua
from asyncua.common.node import Node

from dtdc_simulator.config.envelope import EquipmentEnvelope, load_envelope
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.mv import Mode
from dtdc_simulator.interfaces.opcua.address_space import (
    STAGE_SIGNAL_ATTR,
    compute_active_mask,
    placeholder_double,
    placeholder_string,
)
from dtdc_simulator.interfaces.opcua.certs import APP_URI, SecurityConfig, SecurityMode

logger = logging.getLogger(__name__)

ENDPOINT = "opc.tcp://0.0.0.0:4840/dtdc/"
NAMESPACE_URI = "http://dtdc.sim/"
REFRESH_S = 0.2


def _flatten_constants(prefix: str, data: dict):
    """Yield ``(dotted_path, scalar)`` for every leaf of a ``model_dump()`` dict,
    matching ``_add_const_tree``'s node paths so the push can address them."""
    for key, value in data.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            yield from _flatten_constants(path, value)
        elif isinstance(value, (bool, int, float, str)):
            yield path, value


def _kpi_values(
    outputs,
) -> dict[str, float]:  # outputs: core.model.Outputs (data, not a core/ import)
    """The KPI node name -> value map, single source of truth for both the
    build and push passes so the two can't drift."""
    return {
        "residual_hexane": outputs.kpi_residual_hexane_ppm,
        "meal_moisture": outputs.kpi_meal_moisture_pct,
        "steam_consumption": outputs.kpi_steam_consumption_kg_per_t,
        "throughput": outputs.kpi_throughput_t_per_day,
        "exhaust_hexane": outputs.kpi_exhaust_hexane_ppm,
        "direct_steam": outputs.kpi_direct_steam_kg_s,
        "indirect_heating": outputs.kpi_indirect_heating_kw,
        "drying_air_heating": outputs.kpi_drying_air_heating_kw,
        "total_energy": outputs.kpi_total_energy_kw,
        "outlet_vapor": outputs.kpi_outlet_vapor_kg_s,
        "outlet_vapor_hexane": outputs.kpi_outlet_vapor_hexane_kg_s,
        "outlet_vapor_water": outputs.kpi_outlet_vapor_water_kg_s,
        "condenser_duty": outputs.kpi_condenser_duty_kw,
    }


class OpcUaAdapter:
    def __init__(
        self,
        facade: RuntimeFacade,
        endpoint: str = ENDPOINT,
        envelope: EquipmentEnvelope | None = None,
    ) -> None:
        self._facade = facade
        self._endpoint = endpoint
        self._envelope = envelope if envelope is not None else load_envelope()
        self._server: Server | None = None
        self._idx = 0
        # Canonical-keyed node references (fixed superset, built once).
        self._control_nodes: dict[str, dict[str, Node]] = {}
        self._control_present: dict[str, Node] = {}
        self._raw_mv_nodes: dict[str, dict[str, Node]] = {}
        self._raw_mv_present: dict[str, Node] = {}
        self._dv_nodes: dict[str, Node] = {}
        self._pv_stage_nodes: dict[str, dict[str, Node]] = {}
        self._pv_stage_present: dict[str, Node] = {}
        self._pv_kpi_nodes: dict[str, Node] = {}
        self._sim_nodes: dict[str, Node] = {}
        self._config_nodes: dict[str, Node] = {}
        # Constants/ (BuildSpec §9.1): flat map "Physical.<...>"/"Model.<...>" ->
        # node, plus the canonical Geometry/Stage superset.
        self._constants_nodes: dict[str, Node] = {}
        self._geom_stage_nodes: dict[str, dict[str, Node]] = {}
        self._geom_stage_present: dict[str, Node] = {}
        # change-detection trackers (keyed by CANONICAL name)
        self._last_global_mode = Mode.MANUAL.value
        self._last_speed_factor: float | None = None
        self._last_dt_resolve_interval_s: float | None = None
        self._last_control_mode: dict[str, str] = {}
        self._last_control_sp: dict[str, float] = {}
        self._last_control_op: dict[str, float] = {}
        self._last_dv: dict[str, float] = {}

    # ------------------------------------------------------------------ build
    async def build(self, security: SecurityConfig | None = None) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self._endpoint)
        # Match the server's application URI to the self-signed cert's SAN URI so
        # secure clients don't warn about a URI mismatch (certs.APP_URI).
        await server.set_application_uri(APP_URI)
        await self._apply_security(server, security)
        self._idx = await server.register_namespace(NAMESPACE_URI)
        self._server = server

        dtdc = await server.nodes.objects.add_object(self._idx, "DTDC")
        await self._build_config_folder(dtdc)
        await self._build_constants_folder(dtdc)
        await self._build_sim_folder(dtdc)
        await self._build_control_folder(dtdc)
        await self._build_dv_folder(dtdc)
        await self._build_pv_folder(dtdc)
        await self._build_diagnostics_folder(dtdc)

        # Seed every node's value + the change-detection trackers from the
        # current snapshot BEFORE the run loop's first pull, so the first pull
        # doesn't read build-time defaults back onto the facade (§9.2).
        await self._push_snapshot()

    async def _apply_security(self, server: Server, security: SecurityConfig | None) -> None:
        """Configure the server's security policy (Phase 3). ``None``/``NONE`` is
        anonymous + no encryption; ``BASIC256SHA256`` loads the server cert/key
        for Sign & Encrypt and optionally validates client certs."""
        if security is None or security.mode is SecurityMode.NONE:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
            return
        if security.cert_file is None or security.key_file is None:
            raise ValueError("Basic256Sha256 security requires a server certificate and key")
        await server.load_certificate(str(security.cert_file))
        await server.load_private_key(str(security.key_file))
        # Offer the encrypted policy AND NoSecurity (the "None + Basic256Sha256"
        # mode): NoSecurity keeps discovery + anonymous sandbox access working
        # while clients that want encryption use Basic256Sha256 Sign & Encrypt.
        server.set_security_policy(
            [
                ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
                ua.SecurityPolicyType.NoSecurity,
            ]
        )
        if security.validator is not None:
            server.set_certificate_validator(security.validator)

    async def refresh(self) -> None:
        """One pull-then-push cycle (§9.2). Used by the lifecycle service's loop."""
        await self._pull_writes()
        await self._push_snapshot()

    async def _build_config_folder(self, parent: Node) -> None:
        idx = self._idx
        config = await parent.add_object(idx, "Config")
        self._config_nodes["EnvelopeVersion"] = await config.add_variable(
            idx, "EnvelopeVersion", int(self._envelope.version)
        )
        self._config_nodes["BuildManifest"] = await config.add_variable(idx, "BuildManifest", "{}")
        self._config_nodes["ActiveStageCount"] = await config.add_variable(
            idx, "ActiveStageCount", 0
        )

    async def _build_constants_folder(self, parent: Node) -> None:
        """Cold-config provenance (BuildSpec §9.1), read-only. Physical/Model
        node STRUCTURE is fixed by the pydantic schema (config-independent);
        only values change per build. Geometry uses the canonical stage
        superset, so its paths are stable too. Writing constants (only legal in
        CONFIGURED, via Reconfigure) is a later milestone — these are RO here."""
        idx = self._idx
        constants = await parent.add_object(idx, "Constants")
        cold = self._facade.get_cold_config()
        if cold is not None:
            physical_obj = await constants.add_object(idx, "Physical")
            await self._add_const_tree(physical_obj, "Physical", cold.physical)
            model_obj = await constants.add_object(idx, "Model")
            await self._add_const_tree(model_obj, "Model", cold.model)

        geometry = await constants.add_object(idx, "Geometry")
        geom_stage_root = await geometry.add_object(idx, "Stage")
        for stage in self._envelope.canonical_stages():
            obj = await geom_stage_root.add_object(idx, stage.canonical_id)
            self._geom_stage_nodes[stage.canonical_id] = {
                "Role": await obj.add_variable(idx, "Role", stage.role.value),
                "Diameter": await obj.add_variable(idx, "Diameter", 0.0),
                "BedHeight": await obj.add_variable(idx, "BedHeight", 0.0),
                "VaporPath": await obj.add_variable(idx, "VaporPath", ""),
                "ArmMixing": await obj.add_variable(idx, "ArmMixing", 0.0),
            }
            self._geom_stage_present[stage.canonical_id] = await obj.add_variable(
                idx, "Present", False
            )

    async def _add_const_tree(self, parent: Node, path_prefix: str, data: dict) -> None:
        """Recursively mirror a ``model_dump()`` dict into RO nodes; nested
        param groups (GAB, Antoine, ...) become sub-objects. Node references are
        recorded in ``_constants_nodes`` under their dotted path for the push."""
        idx = self._idx
        for key, value in data.items():
            path = f"{path_prefix}.{key}"
            if isinstance(value, dict):
                sub = await parent.add_object(idx, key)
                await self._add_const_tree(sub, path, value)
            elif isinstance(value, bool):
                self._constants_nodes[path] = await parent.add_variable(idx, key, bool(value))
            elif isinstance(value, (int, float, str)):
                self._constants_nodes[path] = await parent.add_variable(idx, key, value)
            # None / other -> no node (e.g. an unset optional like x2_critical)

    async def _build_sim_folder(self, parent: Node) -> None:
        idx = self._idx
        sim = await parent.add_object(idx, "Simulation")
        snap = self._facade.get_snapshot()

        speed = await sim.add_variable(idx, "SpeedFactor", float(snap.speed_factor))
        await speed.set_writable()
        self._last_speed_factor = float(snap.speed_factor)
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

    async def _build_control_folder(self, parent: Node) -> None:
        """The FULL canonical loop superset (envelope.canonical_control_tags),
        independent of the loaded build. Units/Description are static metadata
        set here for every loop; the live SP/PV/OP/Mode/Status and the Present
        flag are set each push from the active mask."""
        idx = self._idx
        control_root = await parent.add_object(idx, "Control")
        meta = self._envelope.control_loop_meta()
        for tag in self._envelope.canonical_control_tags():
            units, desc = meta.get(tag, ("", ""))
            node_parent = await control_root.add_object(idx, tag)
            mode_n = await node_parent.add_variable(idx, "Mode", Mode.MANUAL.value)
            await mode_n.set_writable()
            sp_n = await node_parent.add_variable(idx, "SP", 0.0)
            await sp_n.set_writable()
            pv_n = await node_parent.add_variable(idx, "PV", 0.0)
            op_n = await node_parent.add_variable(idx, "OP", 0.0)
            await op_n.set_writable()
            units_n = await node_parent.add_variable(idx, "Units", units)
            status_n = await node_parent.add_variable(idx, "Status", "")
            desc_n = await node_parent.add_variable(idx, "Description", desc)
            min_n = await node_parent.add_variable(idx, "Min", 0.0)
            max_n = await node_parent.add_variable(idx, "Max", 0.0)
            present_n = await node_parent.add_variable(idx, "Present", False)

            self._control_nodes[tag] = {
                "Mode": mode_n,
                "SP": sp_n,
                "PV": pv_n,
                "OP": op_n,
                "Units": units_n,
                "Status": status_n,
                "Description": desc_n,
                "Min": min_n,
                "Max": max_n,
            }
            self._control_present[tag] = present_n

    async def _build_dv_folder(self, parent: Node) -> None:
        idx = self._idx
        dv_root = await parent.add_object(idx, "SimulationInputs")
        for dv in self._envelope.disturbances:
            n = await dv_root.add_variable(idx, dv.key, 0.0)
            await n.set_writable()
            self._dv_nodes[dv.key] = n

    async def _build_pv_folder(self, parent: Node) -> None:
        idx = self._idx
        pv_root = await parent.add_object(idx, "Measurements")
        stage_root = await pv_root.add_object(idx, "Stage")
        kpi_root = await pv_root.add_object(idx, "KPI")

        for stage in self._envelope.canonical_stages():
            stage_obj = await stage_root.add_object(idx, stage.canonical_id)
            node_map: dict[str, Node] = {}
            for signal in stage.signals:
                node_map[signal] = await stage_obj.add_variable(idx, signal, 0.0)
            # Role is fixed by the envelope (independent of the build); set once.
            await stage_obj.add_variable(idx, "Role", stage.role.value)
            self._pv_stage_present[stage.canonical_id] = await stage_obj.add_variable(
                idx, "Present", False
            )
            self._pv_stage_nodes[stage.canonical_id] = node_map

        for name in self._envelope.kpis:
            self._pv_kpi_nodes[name] = await kpi_root.add_variable(idx, name, 0.0)

    async def _build_diagnostics_folder(self, parent: Node) -> None:
        """Raw model MVs exposed read-only for commissioning/debugging, over the
        FULL canonical MV superset. Not the PLC integration surface; writable
        process control lives exclusively under ``Control/<loop-tag>``."""
        idx = self._idx
        diagnostics = await parent.add_object(idx, "Diagnostics")
        raw_root = await diagnostics.add_object(idx, "InternalMV")
        for ckey in self._envelope.canonical_mv_keys():
            obj = await raw_root.add_object(idx, ckey.replace("/", "__"))
            self._raw_mv_nodes[ckey] = {
                "Mode": await obj.add_variable(idx, "Mode", Mode.MANUAL.value),
                "ManualSetpoint": await obj.add_variable(idx, "ManualSetpoint", 0.0),
                "AutoSetpoint": await obj.add_variable(idx, "AutoSetpoint", 0.0),
                "EffectiveValue": await obj.add_variable(idx, "EffectiveValue", 0.0),
            }
            self._raw_mv_present[ckey] = await obj.add_variable(idx, "Present", False)

    # ------------------------------------------------------------------ pull
    async def _pull_writes(self) -> None:
        """Apply changed writable nodes to the facade. Only ACTIVE control loops
        and disturbances are routed; placeholder loops are ignored. A node's
        value is applied only if it changed since our own last push wrote it, so
        a UI-originated facade change isn't stomped by a stale node copy (§9.2)."""
        facade = self._facade
        snap = facade.get_snapshot()
        mask = compute_active_mask(self._envelope, snap)

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
                logger.warning("Simulation/GlobalMode: invalid mode %r", global_mode_val)
            self._last_global_mode = global_mode_val

        for tag, nodes in self._control_nodes.items():
            build_tag = mask.control.get(tag)
            if build_tag is None:  # placeholder loop -- ignore client writes
                continue

            mode_val = await nodes["Mode"].read_value()
            if mode_val != self._last_control_mode.get(tag):
                try:
                    facade.set_control_mode(build_tag, Mode(mode_val))
                except ValueError:
                    logger.warning("Control/%s/Mode: invalid mode %r", tag, mode_val)
                self._last_control_mode[tag] = mode_val

            sp_val = float(await nodes["SP"].read_value())
            if sp_val != self._last_control_sp.get(tag):
                facade.set_control_setpoint(build_tag, sp_val)
                self._last_control_sp[tag] = sp_val

            op_val = float(await nodes["OP"].read_value())
            if op_val != self._last_control_op.get(tag):
                facade.set_control_output(build_tag, op_val)
                self._last_control_op[tag] = op_val

        for key, node in self._dv_nodes.items():
            if key not in snap.dvs:
                continue
            val = float(await node.read_value())
            if val != self._last_dv.get(key):
                facade.set_dv(key, val)
                self._last_dv[key] = val

    # ------------------------------------------------------------------ push
    async def _push_snapshot(self) -> None:
        snap = self._facade.get_snapshot()
        mask = compute_active_mask(self._envelope, snap)
        outputs = snap.outputs

        # ---- Simulation (fixed) ----
        await self._sim_nodes["SimTime"].write_value(float(snap.sim_time))
        await self._sim_nodes["ActualSpeed"].write_value(float(snap.actual_speed))
        await self._sim_nodes["State"].write_value(snap.state.value)
        await self._sim_nodes["SpeedFactor"].write_value(float(snap.speed_factor))
        await self._sim_nodes["DTResolveIntervalS"].write_value(float(snap.dt_resolve_interval_s))
        await self._sim_nodes["UndersampleWarning"].write_value(bool(snap.undersample_warning))
        await self._sim_nodes["SolverStress"].write_value(bool(snap.solver_stress))
        if outputs is not None:
            await self._sim_nodes["DTSolverOuterIterations"].write_value(
                int(outputs.dt_solver_outer_iterations)
            )
        self._last_speed_factor = float(snap.speed_factor)
        self._last_dt_resolve_interval_s = float(snap.dt_resolve_interval_s)

        # ---- Config / build manifest ----
        await self._config_nodes["BuildManifest"].write_value(mask.manifest_json)
        await self._config_nodes["ActiveStageCount"].write_value(len(mask.active_stage_ids()))

        # ---- Measurements/Stage (superset) ----
        kpi_map = _kpi_values(outputs) if outputs is not None else None
        for cid, nodes in self._pv_stage_nodes.items():
            build_id = mask.stage.get(cid)
            active = build_id is not None and outputs is not None and build_id in outputs.stage_T
            await self._pv_stage_present[cid].write_value(bool(build_id is not None))
            if active:
                for signal, node in nodes.items():
                    attr = STAGE_SIGNAL_ATTR[signal]
                    await node.write_value(float(getattr(outputs, attr)[build_id]))
            else:
                for node in nodes.values():
                    await node.write_value(placeholder_double())

        for name, node in self._pv_kpi_nodes.items():
            if kpi_map is not None:
                await node.write_value(float(kpi_map[name]))
            else:
                await node.write_value(placeholder_double())

        # ---- Control (superset) ----
        for tag, nodes in self._control_nodes.items():
            build_tag = mask.control.get(tag)
            await self._control_present[tag].write_value(bool(build_tag is not None))
            if build_tag is not None:
                loop = snap.control_loops[build_tag]
                await nodes["Mode"].write_value(loop.mode)
                await nodes["SP"].write_value(float(loop.sp))
                await nodes["PV"].write_value(float(loop.pv))
                await nodes["OP"].write_value(float(loop.op))
                await nodes["Status"].write_value(loop.status)
                await nodes["Min"].write_value(float(loop.minimum))
                await nodes["Max"].write_value(float(loop.maximum))
                self._last_control_mode[tag] = loop.mode
                self._last_control_sp[tag] = float(loop.sp)
                self._last_control_op[tag] = float(loop.op)
            else:
                await nodes["Mode"].write_value(placeholder_string())
                await nodes["Status"].write_value(placeholder_string())
                for field in ("SP", "PV", "OP", "Min", "Max"):
                    await nodes[field].write_value(placeholder_double())

        # ---- Diagnostics/InternalMV (superset, read-only) ----
        for ckey, nodes in self._raw_mv_nodes.items():
            build_key = mask.mv.get(ckey)
            await self._raw_mv_present[ckey].write_value(bool(build_key is not None))
            if build_key is not None:
                mv = snap.mvs[build_key]
                await nodes["Mode"].write_value(mv.mode.value)
                await nodes["ManualSetpoint"].write_value(float(mv.manual_setpoint))
                await nodes["AutoSetpoint"].write_value(float(mv.auto_setpoint))
                await nodes["EffectiveValue"].write_value(float(mv.effective_value))
            else:
                await nodes["Mode"].write_value(placeholder_string())
                for field in ("ManualSetpoint", "AutoSetpoint", "EffectiveValue"):
                    await nodes[field].write_value(placeholder_double())

        # ---- SimulationInputs / DVs (always active) ----
        for key, node in self._dv_nodes.items():
            if key not in snap.dvs:
                await node.write_value(placeholder_double())
                continue
            value = float(snap.dvs[key])
            await node.write_value(value)
            self._last_dv[key] = value

        # ---- Constants (provenance; values refreshed, structure fixed) ----
        await self._push_constants(mask)

    async def _push_constants(self, mask) -> None:
        cold = self._facade.get_cold_config()
        if cold is None:
            return  # transient reconfigure -- keep last-known constants
        for prefix, data in (("Physical", cold.physical), ("Model", cold.model)):
            for path, value in _flatten_constants(prefix, data):
                node = self._constants_nodes.get(path)
                if node is not None:
                    await node.write_value(value)

        for cid, nodes in self._geom_stage_nodes.items():
            build_id = mask.stage.get(cid)
            geo = cold.geometry.get(build_id) if build_id is not None else None
            await self._geom_stage_present[cid].write_value(bool(build_id is not None))
            if geo is not None:
                await nodes["Role"].write_value(geo.role)
                await nodes["Diameter"].write_value(float(geo.diameter_m))
                await nodes["BedHeight"].write_value(float(geo.bed_height_m))
                await nodes["VaporPath"].write_value(geo.vapor_path)
                await nodes["ArmMixing"].write_value(float(geo.arm_mixing_factor))
            else:
                await nodes["Role"].write_value(placeholder_string())
                await nodes["VaporPath"].write_value(placeholder_string())
                for f in ("Diameter", "BedHeight", "ArmMixing"):
                    await nodes[f].write_value(placeholder_double())

    async def run(self) -> None:
        assert self._server is not None
        async with self._server:
            while not self._facade.is_shutdown():
                try:
                    await self.refresh()
                except Exception:
                    logger.exception("OPC UA refresh cycle failed")
                await asyncio.sleep(REFRESH_S)


async def serve(facade: RuntimeFacade, endpoint: str = ENDPOINT) -> None:
    """Build the fixed superset address space from the equipment envelope and
    serve forever (anonymous, no security). Retained for simple/headless use;
    the GUI drives the richer lifecycle via ``interfaces/opcua/service.py``."""
    adapter = OpcUaAdapter(facade, endpoint)
    await adapter.build()
    logger.info("OPC UA server listening on %s (SecurityPolicy#None, anonymous)", endpoint)
    await adapter.run()
