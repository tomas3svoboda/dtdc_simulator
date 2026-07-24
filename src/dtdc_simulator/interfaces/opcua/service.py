"""OPC UA server lifecycle service (Phase 3).

Wraps the ``OpcUaAdapter`` so the GUI can start/stop/restart the server at
runtime, change the bind endpoint, and switch security modes — without tearing
down the simulator. The server runs as an asyncio task in NiceGUI's own event
loop (single loop; the tick loop stays on its worker thread). Endpoint/security
changes require the server to be stopped, so the GUI disables those controls
while it is running.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

from dtdc_simulator.config.envelope import EquipmentEnvelope, load_envelope
from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.interfaces.opcua.certs import (
    PkiPaths,
    SecurityMode,
    list_trusted,
    make_security_config,
)
from dtdc_simulator.interfaces.opcua.server import REFRESH_S, OpcUaAdapter

logger = logging.getLogger(__name__)


class ServerState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


@dataclass
class Endpoint:
    host: str = "0.0.0.0"
    port: int = 4840
    path: str = "/dtdc/"

    def url(self) -> str:
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        return f"opc.tcp://{self.host}:{self.port}{path}"

    def cert_host(self) -> str:
        # 0.0.0.0 is a bind wildcard, not a name a cert SAN can use.
        return "localhost" if self.host in ("0.0.0.0", "") else self.host

    @classmethod
    def parse(cls, url: str) -> "Endpoint":
        """Parse an ``opc.tcp://host:port/path`` URL into an Endpoint."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return cls(
            host=parsed.hostname or "0.0.0.0",
            port=parsed.port or 4840,
            path=parsed.path or "/dtdc/",
        )


@dataclass
class ServiceStatus:
    state: ServerState
    endpoint_url: str
    security_mode: SecurityMode
    require_client_trust: bool
    trusted_certs: list[str] = field(default_factory=list)
    error: str = ""


class OpcUaService:
    def __init__(
        self,
        facade: RuntimeFacade,
        envelope: EquipmentEnvelope | None = None,
        pki_root: str = "pki",
    ) -> None:
        self._facade = facade
        self._envelope = envelope if envelope is not None else load_envelope()
        self._pki = PkiPaths.at(pki_root)
        self._endpoint = Endpoint()
        self._security_mode = SecurityMode.NONE
        self._require_client_trust = False
        self._state = ServerState.STOPPED
        self._error = ""
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready: asyncio.Event | None = None
        self._adapter: OpcUaAdapter | None = None

    # ------------------------------------------------------------ introspection
    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def pki(self) -> PkiPaths:
        return self._pki

    @property
    def envelope(self) -> EquipmentEnvelope:
        return self._envelope

    @property
    def endpoint(self) -> Endpoint:
        return self._endpoint

    def status(self) -> ServiceStatus:
        return ServiceStatus(
            state=self._state,
            endpoint_url=self._endpoint.url(),
            security_mode=self._security_mode,
            require_client_trust=self._require_client_trust,
            trusted_certs=list_trusted(self._pki),
            error=self._error,
        )

    # ------------------------------------------------------------ configuration
    def _require_stopped(self) -> None:
        if self._state in (ServerState.RUNNING, ServerState.STARTING):
            raise RuntimeError("stop the OPC UA server before changing its configuration")

    def set_endpoint(self, host: str, port: int, path: str = "/dtdc/") -> None:
        self._require_stopped()
        self._endpoint = Endpoint(host=host.strip(), port=int(port), path=path.strip() or "/dtdc/")

    def set_security(self, mode: SecurityMode, require_client_trust: bool = False) -> None:
        self._require_stopped()
        self._security_mode = mode
        self._require_client_trust = require_client_trust

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._state in (ServerState.RUNNING, ServerState.STARTING):
            return
        self._error = ""
        self._state = ServerState.STARTING
        self._stop_event = asyncio.Event()
        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._serve())
        await self._ready.wait()
        if self._state is ServerState.ERROR:
            raise RuntimeError(self._error)

    async def _serve(self) -> None:
        assert self._stop_event is not None and self._ready is not None
        try:
            security = await make_security_config(
                self._security_mode,
                self._pki,
                require_client_trust=self._require_client_trust,
                host_name=self._endpoint.cert_host(),
            )
            adapter = OpcUaAdapter(self._facade, self._endpoint.url(), self._envelope)
            await adapter.build(security)
            self._adapter = adapter
            async with adapter._server:
                self._state = ServerState.RUNNING
                logger.info(
                    "OPC UA server listening on %s (%s)",
                    self._endpoint.url(),
                    self._security_mode.value,
                )
                self._ready.set()
                while not self._stop_event.is_set() and not self._facade.is_shutdown():
                    try:
                        await adapter.refresh()
                    except Exception:
                        logger.exception("OPC UA refresh cycle failed")
                    await asyncio.sleep(REFRESH_S)
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI via status().error
            logger.exception("OPC UA server failed")
            self._error = f"{type(exc).__name__}: {exc}"
            self._state = ServerState.ERROR
        finally:
            self._adapter = None
            if self._state is not ServerState.ERROR:
                self._state = ServerState.STOPPED
            self._ready.set()  # unblock start() even on an early failure

    async def stop(self) -> None:
        if self._task is None:
            self._state = ServerState.STOPPED
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=10.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        self._stop_event = None
        if self._state is not ServerState.ERROR:
            self._state = ServerState.STOPPED

    async def restart(self) -> None:
        await self.stop()
        await self.start()
