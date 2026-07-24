"""Process entrypoint: wires config -> engine -> interfaces (BuildSpec §3).

Starts the tick-loop worker thread, then hands control to NiceGUI (which
launches the OPC UA server as an asyncio task inside its own event loop —
see interfaces/ui/app.py). Run with `dtdc-sim` (see pyproject.toml) or
`python -m dtdc_simulator.main`.

NiceGUI and the heavy adapters are imported inside `main()`, AFTER
`_patch_windows_ssl()` runs, because importing NiceGUI (via python-socketio)
builds an SSL default context at import time — see that function's docstring.
"""

from __future__ import annotations

import argparse
import logging
import ssl
import sys

DEFAULT_SCENARIO = "scenarios/soybean_default.yaml"
# Mirrors interfaces/opcua/server.ENDPOINT; kept here as a plain constant so
# argument parsing doesn't force an early import of the OPC UA/asyncua stack.
DEFAULT_OPCUA_ENDPOINT = "opc.tcp://0.0.0.0:4840/dtdc/"


def _patch_windows_ssl() -> None:
    """Work around a Windows cert-store hang before importing NiceGUI.

    NiceGUI's import chain (via ``python-socketio``) constructs an SSL default
    context at import time. On some Windows machines — typically domain-joined —
    ``ssl.SSLContext.load_default_certs`` enumerates the Windows certificate
    store (``_load_windows_store_certs``) and can hang for a long time, so the
    app never reaches "ready". This is a local sandbox HMI that needs no
    corporate root store, so we load CA certificates from ``certifi`` instead,
    which is fast and hang-free. No-op off Windows (OpenSSL's default paths are
    already fast there) and if ``certifi`` is unavailable.
    """
    if sys.platform != "win32":
        return
    try:
        import certifi
    except ImportError:
        return

    def _load_default_certs(self: ssl.SSLContext, purpose=ssl.Purpose.SERVER_AUTH) -> None:
        self.load_verify_locations(cafile=certifi.where())

    ssl.SSLContext.load_default_certs = _load_default_certs  # type: ignore[method-assign]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DTDC real-time simulator")
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        help="Default scenario YAML shown on the setup screen",
    )
    parser.add_argument("--host", default="127.0.0.1", help="NiceGUI dashboard host")
    parser.add_argument("--port", type=int, default=8080, help="NiceGUI dashboard port")
    parser.add_argument("--no-opcua", action="store_true", help="Disable the OPC UA server")
    parser.add_argument(
        "--opcua-endpoint", default=DEFAULT_OPCUA_ENDPOINT, help="OPC UA endpoint URL"
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _patch_windows_ssl()  # must precede the NiceGUI import below
    args = parse_args()

    from nicegui import app, ui

    from dtdc_simulator.engine.facade import RuntimeFacade
    from dtdc_simulator.engine.loop import start_background_thread
    from dtdc_simulator.interfaces.ui.app import create_app

    facade = RuntimeFacade()
    start_background_thread(facade)
    app.on_shutdown(facade.shutdown)

    create_app(
        facade,
        default_scenario=args.scenario,
        opcua_endpoint=None if args.no_opcua else args.opcua_endpoint,
    )

    ui.run(host=args.host, port=args.port, title="DTDC Simulator", reload=False, show=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
