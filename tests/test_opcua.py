import asyncio

import pytest

from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ClockKind
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.interfaces.opcua.server import OpcUaAdapter


def _facade() -> RuntimeFacade:
    cfg = load_scenario("scenarios/soybean_default.yaml")
    cfg.sim.clock = ClockKind.FREERUN
    cfg.model.dt_nz_phz = 5
    cfg.model.dt_nz_ftrz = 5
    cfg.model.dt_nz_dcz = 4
    facade = RuntimeFacade()
    facade.configure(cfg)
    facade.assemble()
    return facade


def test_plc_address_space_and_control_write_routing() -> None:
    async def exercise() -> None:
        facade = _facade()
        adapter = OpcUaAdapter(facade, "opc.tcp://127.0.0.1:4851/test/")
        await adapter.build()

        assert adapter._server is not None
        object_names = {
            (await node.read_browse_name()).Name
            for node in await adapter._server.nodes.objects.get_children()
        }
        assert "DTDC" in object_names
        assert "FIC_DT_PD_IND_STM" in adapter._control_nodes
        assert set(adapter._control_nodes["FIC_DT_PD_IND_STM"]) >= {
            "Mode",
            "SP",
            "PV",
            "OP",
            "Units",
            "Status",
        }
        assert "indirect_steam/PD1" in adapter._raw_mv_nodes

        nodes = adapter._control_nodes["FIC_DT_PD_IND_STM"]
        await nodes["Mode"].write_value("AUTO")
        await nodes["SP"].write_value(1500.0)
        await adapter._pull_writes()

        loop = facade.get_snapshot().control_loops["FIC_DT_PD_IND_STM"]
        assert loop.mode == "AUTO"
        assert loop.sp == pytest.approx(1500.0)

    asyncio.run(exercise())
