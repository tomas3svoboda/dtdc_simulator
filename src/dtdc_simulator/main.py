"""Process entrypoint: wires config -> engine -> interfaces (BuildSpec §3).

Starts the tick-loop worker thread, then hands control to NiceGUI (which
launches the OPC UA server as an asyncio task inside its own event loop —
see interfaces/ui/app.py). Run with `dtdc-sim` (see pyproject.toml) or
`python -m dtdc_simulator.main`.
"""

from __future__ import annotations
import argparse
import logging

from nicegui import app, ui

from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.engine.loop import start_background_thread
from dtdc_simulator.interfaces.opcua.server import ENDPOINT as OPCUA_ENDPOINT
from dtdc_simulator.interfaces.ui.app import create_app

DEFAULT_SCENARIO = "scenarios/soybean_default.yaml"


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
    parser.add_argument("--opcua-endpoint", default=OPCUA_ENDPOINT, help="OPC UA endpoint URL")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()

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
