"""OPC UA lifecycle service + certificate/PKI helpers (Phase 3)."""

from __future__ import annotations

import asyncio

import pytest
from asyncua import Client

from dtdc_simulator.config.loader import load_scenario
from dtdc_simulator.config.schema import ClockKind
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.interfaces.opcua.certs import (
    PkiPaths,
    SecurityMode,
    list_trusted,
    make_security_config,
    save_trusted_cert,
)
from dtdc_simulator.interfaces.opcua.service import Endpoint, OpcUaService, ServerState


@pytest.fixture(scope="module")
def facade() -> RuntimeFacade:
    cfg = load_scenario("scenarios/soybean_default.yaml")
    cfg.sim.clock = ClockKind.FREERUN
    cfg.model.dt_nz_phz = 5
    cfg.model.dt_nz_ftrz = 5
    cfg.model.dt_nz_dcz = 4
    f = RuntimeFacade()
    f.configure(cfg)
    f.assemble()
    return f


def test_endpoint_parse_and_cert_host() -> None:
    e = Endpoint.parse("opc.tcp://1.2.3.4:4855/dtdc/")
    assert (e.host, e.port, e.path) == ("1.2.3.4", 4855, "/dtdc/")
    assert e.cert_host() == "1.2.3.4"
    assert Endpoint(host="0.0.0.0").cert_host() == "localhost"


def test_service_start_client_read_stop(facade, tmp_path) -> None:
    async def exercise() -> None:
        svc = OpcUaService(facade, pki_root=str(tmp_path / "pki"))
        svc.set_endpoint("127.0.0.1", 4891, "/dtdc/")
        await svc.start()
        assert svc.state is ServerState.RUNNING
        async with Client(svc.status().endpoint_url) as client:
            node = await client.nodes.root.get_child(
                ["0:Objects", "2:DTDC", "2:Config", "2:ActiveStageCount"]
            )
            assert await node.get_value() == 8
        await svc.stop()
        assert svc.state is ServerState.STOPPED

    asyncio.run(exercise())


def test_config_locked_while_running(facade, tmp_path) -> None:
    async def exercise() -> None:
        svc = OpcUaService(facade, pki_root=str(tmp_path / "pki"))
        svc.set_endpoint("127.0.0.1", 4892, "/dtdc/")
        await svc.start()
        with pytest.raises(RuntimeError):
            svc.set_endpoint("127.0.0.1", 4999, "/x/")
        with pytest.raises(RuntimeError):
            svc.set_security(SecurityMode.BASIC256SHA256)
        await svc.stop()

    asyncio.run(exercise())


def test_encrypted_mode_advertises_basic256sha256(facade, tmp_path) -> None:
    async def exercise() -> None:
        svc = OpcUaService(facade, pki_root=str(tmp_path / "pki"))
        svc.set_endpoint("127.0.0.1", 4893, "/dtdc/")
        svc.set_security(SecurityMode.BASIC256SHA256)
        await svc.start()
        assert svc.state is ServerState.RUNNING
        async with Client(svc.status().endpoint_url) as client:
            endpoints = await client.connect_and_get_server_endpoints()
        policies = {e.SecurityPolicyUri.split("#")[-1] for e in endpoints}
        assert "Basic256Sha256" in policies
        assert "None" in policies  # discovery/anonymous still available
        await svc.stop()

    asyncio.run(exercise())


def test_restart(facade, tmp_path) -> None:
    async def exercise() -> None:
        svc = OpcUaService(facade, pki_root=str(tmp_path / "pki"))
        svc.set_endpoint("127.0.0.1", 4894, "/dtdc/")
        await svc.start()
        await svc.restart()
        assert svc.state is ServerState.RUNNING
        await svc.stop()

    asyncio.run(exercise())


def test_cert_generation_and_trust_store(tmp_path) -> None:
    async def exercise() -> None:
        pki = PkiPaths.at(tmp_path / "pki")
        sc = await make_security_config(SecurityMode.BASIC256SHA256, pki)
        assert sc.cert_file.exists() and sc.key_file.exists()
        assert sc.cert_file.stat().st_size > 0
        save_trusted_cert(pki, "client.der", b"dummy-cert-bytes")
        assert "client.der" in list_trusted(pki)

    asyncio.run(exercise())


def test_save_trusted_cert_rejects_path_traversal(tmp_path) -> None:
    pki = PkiPaths.at(tmp_path / "pki")
    dest = save_trusted_cert(pki, "../../evil.der", b"x")
    assert dest.parent == pki.trusted_dir  # basename only; upload can't escape
    assert dest.name == "evil.der"
