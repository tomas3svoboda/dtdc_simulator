"""Strict superset address space: node paths are stable across reconfiguration;
inactive slots are present-but-placeholder (Bad quality + Present=false)."""

from __future__ import annotations

import asyncio
import copy
import json

from asyncua import ua

from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ClockKind, ScenarioConfig
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.interfaces.opcua.address_space import compute_active_mask
from dtdc_simulator.interfaces.opcua.server import OpcUaAdapter


def _fast(cfg: ScenarioConfig) -> ScenarioConfig:
    cfg.sim.clock = ClockKind.FREERUN
    cfg.model.dt_nz_phz = 5
    cfg.model.dt_nz_ftrz = 5
    cfg.model.dt_nz_dcz = 4
    return cfg


def _assembled(cfg: ScenarioConfig) -> RuntimeFacade:
    facade = RuntimeFacade()
    facade.configure(_fast(cfg))
    facade.assemble()
    return facade


def _minimal_raw() -> dict:
    """A trimmed 3-stage DT-only build (1 PD / 1 MN / 1 SP, no dryer/cooler)."""
    raw = load_scenario("scenarios/soybean_default.yaml").model_dump()
    raw = copy.deepcopy(raw)
    keep = {"PD1", "MN1", "SP1"}
    raw["geometry"]["stages"] = [s for s in raw["geometry"]["stages"] if s["id"] in keep]
    raw["topology"]["solid_transfers"] = [
        {
            "id": "PD1_TO_MN1",
            "from_stage": "PD1",
            "to_stage": "MN1",
            "device_type": "CONTROLLED_GATE",
            "controlled": True,
        },
        {
            "id": "MN1_TO_SP1",
            "from_stage": "MN1",
            "to_stage": "SP1",
            "device_type": "CONTROLLED_GATE",
            "controlled": True,
        },
        {
            "id": "SP1_PRODUCT",
            "from_stage": "SP1",
            "to_stage": None,
            "device_type": "ROTARY_AIRLOCK",
            "controlled": True,
            "vapor_seal": True,
        },
    ]
    raw["operating_defaults"]["indirect_steam"] = {"PD1": 7.6e5, "MN1": 8.0e4, "SP1": 3.0e4}
    raw["operating_defaults"]["direct_steam"] = {"SP1": 3.9}
    raw["operating_defaults"]["sweep_arm_speed"] = {"PD1": 3.0, "MN1": 3.0, "SP1": 3.0}
    raw["operating_defaults"]["transfer_device_position"] = {
        "PD1_TO_MN1": 50,
        "MN1_TO_SP1": 50,
        "SP1_PRODUCT": 50,
    }
    return raw


async def _variable_paths(adapter: OpcUaAdapter) -> set[str]:
    paths: set[str] = set()

    async def walk(node, prefix: str) -> None:
        for child in await node.get_children():
            name = (await child.read_browse_name()).Name
            path = f"{prefix}/{name}"
            if await child.read_node_class() == ua.NodeClass.Variable:
                paths.add(path)
            await walk(child, path)

    for obj in await adapter._server.nodes.objects.get_children():
        if (await obj.read_browse_name()).Name == "DTDC":
            await walk(obj, "DTDC")
    return paths


def test_node_paths_identical_across_reconfiguration() -> None:
    async def exercise() -> None:
        full = _assembled(load_scenario("scenarios/soybean_default.yaml"))
        a = OpcUaAdapter(full, "opc.tcp://127.0.0.1:4861/a/")
        await a.build()
        paths_full = await _variable_paths(a)

        minimal = _assembled(ScenarioConfig.model_validate(_minimal_raw()))
        b = OpcUaAdapter(minimal, "opc.tcp://127.0.0.1:4862/b/")
        await b.build()
        paths_min = await _variable_paths(b)

        # The whole point of the superset: the tag map never changes.
        assert paths_full == paths_min
        assert len(paths_full) > 300  # full canonical tree, not the trimmed build

    asyncio.run(exercise())


def test_inactive_stage_is_placeholder() -> None:
    async def exercise() -> None:
        minimal = _assembled(ScenarioConfig.model_validate(_minimal_raw()))
        adapter = OpcUaAdapter(minimal, "opc.tcp://127.0.0.1:4863/c/")
        await adapter.build()

        # PD1 active -> Good; DR1 (no dryer in this build) placeholder -> Bad.
        assert await adapter._pv_stage_present["PD1"].read_value() is True
        pd1 = await adapter._pv_stage_nodes["PD1"]["T"].read_data_value(raise_on_bad_status=False)
        assert not pd1.StatusCode.is_bad()

        assert await adapter._pv_stage_present["DR1"].read_value() is False
        dr1 = await adapter._pv_stage_nodes["DR1"]["T"].read_data_value(raise_on_bad_status=False)
        assert dr1.StatusCode.is_bad()

        # PD7 exists in the tree though no build ever fills 7 predesolv here.
        assert "PD7" in adapter._pv_stage_nodes
        pd7 = await adapter._pv_stage_nodes["PD7"]["T"].read_data_value(raise_on_bad_status=False)
        assert pd7.StatusCode.is_bad()

    asyncio.run(exercise())


def test_build_manifest_reports_active_mask() -> None:
    async def exercise() -> None:
        minimal = _assembled(ScenarioConfig.model_validate(_minimal_raw()))
        adapter = OpcUaAdapter(minimal, "opc.tcp://127.0.0.1:4864/d/")
        await adapter.build()

        manifest = json.loads(await adapter._config_nodes["BuildManifest"].read_value())
        assert manifest["active_stages"] == ["PD1", "MN1", "SP1"]
        assert manifest["envelope_version"] == 1
        assert await adapter._config_nodes["ActiveStageCount"].read_value() == 3

    asyncio.run(exercise())


def test_constants_folder_provenance_and_geometry() -> None:
    async def exercise() -> None:
        facade = _assembled(load_scenario("scenarios/soybean_default.yaml"))
        adapter = OpcUaAdapter(facade, "opc.tcp://127.0.0.1:4865/e/")
        await adapter.build()

        # Physical scalars + nested param groups are exposed read-only.
        assert await adapter._constants_nodes["Physical.rho_solid"].read_value() == 1513.0
        assert "Physical.gab_params.Xm" in adapter._constants_nodes  # nested group
        assert "Model.D_ax" in adapter._constants_nodes

        # Geometry uses the canonical superset: PD1 active, PD7 placeholder.
        assert await adapter._geom_stage_present["PD1"].read_value() is True
        assert await adapter._geom_stage_nodes["PD1"]["Diameter"].read_value() == 6.0
        assert await adapter._geom_stage_present["PD7"].read_value() is False
        pd7 = await adapter._geom_stage_nodes["PD7"]["Diameter"].read_data_value(
            raise_on_bad_status=False
        )
        assert pd7.StatusCode.is_bad()

    asyncio.run(exercise())


def test_constants_structure_stable_across_builds() -> None:
    # Physical/Model node paths are schema-fixed, so a smaller build has the
    # SAME constants structure (values differ, paths do not).
    async def exercise() -> None:
        full = _assembled(load_scenario("scenarios/soybean_default.yaml"))
        a = OpcUaAdapter(full, "opc.tcp://127.0.0.1:4866/f/")
        await a.build()
        minimal = _assembled(ScenarioConfig.model_validate(_minimal_raw()))
        b = OpcUaAdapter(minimal, "opc.tcp://127.0.0.1:4867/g/")
        await b.build()
        assert set(a._constants_nodes) == set(b._constants_nodes)

    asyncio.run(exercise())


def test_active_mask_binds_by_role_order() -> None:
    # Pure binding check (no server): canonical slots fill in role order.
    facade = _assembled(load_scenario("scenarios/soybean_default.yaml"))
    from dtdc_simulator.config.envelope import load_envelope

    mask = compute_active_mask(load_envelope(), facade.get_snapshot())
    assert mask.stage["PD1"] == "PD1"
    assert mask.stage["PD3"] == "PD3"
    assert mask.stage["PD4"] is None  # only 3 predesolv trays in the default build
    assert mask.stage["DR1"] == "DR1"
    assert mask.control["FIC_DT_DIRECT_STM"] == "FIC_DT_DIRECT_STM"
    assert mask.mv["direct_steam/SP1"] == "direct_steam/SP1"
    assert mask.mv["indirect_steam/PD4"] is None
