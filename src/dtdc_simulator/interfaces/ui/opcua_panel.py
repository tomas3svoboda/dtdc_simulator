"""NiceGUI panel to control + debug the OPC UA server (Phase 3).

Gives the operator a self-contained place to: start/stop/restart the server,
edit the bind endpoint (IP/port/path), switch security (None vs Basic256Sha256
Sign & Encrypt), download the server's public certificate and upload trusted
client certificates, and browse the live address space (every canonical node
with its active/placeholder status, value and OPC UA quality).

Talks only to `OpcUaService` + `RuntimeFacade` (never `core/`). Endpoint/security
edits are only accepted while the server is stopped, so those controls are
disabled whenever it is running.
"""

from __future__ import annotations

import logging

from nicegui import ui

from dtdc_simulator.engine.facade import RuntimeFacade
from dtdc_simulator.interfaces.opcua.browser import browser_rows
from dtdc_simulator.interfaces.opcua.certs import (
    SecurityMode,
    ensure_server_certificate,
    save_trusted_cert,
)
from dtdc_simulator.interfaces.opcua.service import OpcUaService, ServerState

logger = logging.getLogger(__name__)

_STATE_COLOR = {
    ServerState.RUNNING: "positive",
    ServerState.STARTING: "warning",
    ServerState.STOPPED: "grey",
    ServerState.ERROR: "negative",
}


def create_opcua_panel(service: OpcUaService, facade: RuntimeFacade) -> None:
    """Build the OPC UA control + browser panel in the current UI context."""
    envelope = service.envelope
    filter_state = {"q": ""}

    with ui.column().classes("w-full gap-3"):
        # ---- status + lifecycle -------------------------------------------
        with ui.row().classes("w-full items-center gap-3"):
            state_badge = ui.badge("STOPPED").props("color=grey")
            endpoint_label = ui.label().classes("text-sm font-mono")
            security_label = ui.label().classes("text-sm text-gray-500")
            error_label = ui.label().classes("text-sm text-red-600")

        with ui.row().classes("items-center gap-2"):
            start_btn = ui.button("Start", icon="play_arrow")
            stop_btn = ui.button("Stop", icon="stop").props("color=negative")
            restart_btn = ui.button("Restart", icon="restart_alt").props("outline")

        # ---- endpoint + security (only editable while stopped) ------------
        with ui.card().classes("w-full"):
            ui.label("Endpoint & security").classes("font-semibold")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                host_input = ui.input("Bind IP", value=service.endpoint.host).classes("w-40")
                port_input = ui.number("Port", value=service.endpoint.port, format="%d").classes(
                    "w-28"
                )
                path_input = ui.input("Path", value=service.endpoint.path).classes("w-32")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                security_select = ui.select(
                    {
                        SecurityMode.NONE.value: "None (anonymous)",
                        SecurityMode.BASIC256SHA256.value: "None + Basic256Sha256 (Sign & Encrypt)",
                    },
                    value=service.status().security_mode.value,
                    label="Security",
                ).classes("w-80")
                trust_checkbox = ui.checkbox(
                    "Require trusted client certificate",
                    value=service.status().require_client_trust,
                )
            apply_btn = ui.button("Apply configuration", icon="save").props("outline")

        # ---- certificates -------------------------------------------------
        with ui.card().classes("w-full"):
            ui.label("Certificates (PKI)").classes("font-semibold")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.button(
                    "Download server certificate",
                    icon="download",
                    on_click=lambda: _download_server_cert(service),
                )
                ui.upload(
                    label="Upload trusted client cert",
                    auto_upload=True,
                    on_upload=lambda e: _on_upload_cert(service, e, refresh_status),
                ).props("accept=.der,.pem,.crt,.cer").classes("max-w-xs")
            trusted_label = ui.label().classes("text-xs text-gray-500")

        # ---- node browser -------------------------------------------------
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Address-space browser").classes("font-semibold")
                counts_label = ui.label().classes("text-xs text-gray-500")
            search = ui.input("Filter by path or category").classes("w-full")
            browser_table = ui.table(
                columns=[
                    {
                        "name": "category",
                        "label": "Category",
                        "field": "category",
                        "align": "left",
                        "sortable": True,
                    },
                    {
                        "name": "path",
                        "label": "Path",
                        "field": "path",
                        "align": "left",
                        "sortable": True,
                    },
                    {"name": "active", "label": "Active", "field": "active", "align": "center"},
                    {"name": "value", "label": "Value", "field": "value", "align": "right"},
                    {"name": "quality", "label": "Quality", "field": "quality", "align": "center"},
                ],
                rows=[],
                row_key="path",
                pagination=15,
            ).classes("w-full")

    # ---- behaviour --------------------------------------------------------
    def _config_enabled(state: ServerState) -> bool:
        return state in (ServerState.STOPPED, ServerState.ERROR)

    def refresh_status() -> None:
        status = service.status()
        state = status.state
        state_badge.text = state.value
        state_badge.props(f"color={_STATE_COLOR.get(state, 'grey')}")
        endpoint_label.text = status.endpoint_url
        security_label.text = f"security: {status.security_mode.value}"
        error_label.text = status.error or ""
        trusted = status.trusted_certs
        trusted_label.text = (
            f"Trusted client certs: {', '.join(trusted)}"
            if trusted
            else "Trusted client certs: none"
        )
        editable = _config_enabled(state)
        for el in (host_input, port_input, path_input, security_select, trust_checkbox, apply_btn):
            el.set_enabled(editable)
        start_btn.set_enabled(state in (ServerState.STOPPED, ServerState.ERROR))
        stop_btn.set_enabled(state in (ServerState.RUNNING, ServerState.STARTING))
        restart_btn.set_enabled(state is ServerState.RUNNING)

    async def do_start() -> None:
        try:
            await service.start()
            ui.notify(f"OPC UA server started on {service.status().endpoint_url}", type="positive")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Start failed: {exc}", type="negative")
        refresh_status()

    async def do_stop() -> None:
        await service.stop()
        ui.notify("OPC UA server stopped")
        refresh_status()

    async def do_restart() -> None:
        try:
            await service.restart()
            ui.notify("OPC UA server restarted", type="positive")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Restart failed: {exc}", type="negative")
        refresh_status()

    def do_apply() -> None:
        try:
            service.set_endpoint(host_input.value, int(port_input.value), path_input.value)
            service.set_security(SecurityMode(security_select.value), bool(trust_checkbox.value))
            ui.notify("Configuration applied", type="positive")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Cannot apply: {exc}", type="negative")
        refresh_status()

    start_btn.on_click(do_start)
    stop_btn.on_click(do_stop)
    restart_btn.on_click(do_restart)
    apply_btn.on_click(do_apply)

    def refresh_browser() -> None:
        rows = browser_rows(envelope, facade.get_snapshot())
        q = filter_state["q"].lower()
        shown = [r for r in rows if not q or q in r.path.lower() or q in r.category.lower()]
        browser_table.rows = [
            {
                "category": r.category,
                "path": r.path,
                "active": "●" if r.active else "○",
                "value": r.value,
                "quality": r.quality,
            }
            for r in shown
        ]
        browser_table.update()
        active = sum(1 for r in rows if r.active)
        counts_label.text = (
            f"{active} active · {len(rows) - active} placeholder · {len(rows)} total"
        )

    search.on_value_change(lambda e: (filter_state.update(q=e.value or ""), refresh_browser()))

    refresh_status()
    refresh_browser()
    ui.timer(1.0, refresh_status)
    ui.timer(1.0, refresh_browser)


async def _download_server_cert(service: OpcUaService) -> None:
    try:
        cert_file, _ = await ensure_server_certificate(
            service.pki, host_name=service.endpoint.cert_host()
        )
        ui.download(str(cert_file), "dtdc_server_cert.der")
        ui.notify("Server certificate ready for download", type="positive")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Certificate error: {exc}", type="negative")


def _on_upload_cert(service: OpcUaService, event, on_done) -> None:
    try:
        data = event.content.read()
        save_trusted_cert(service.pki, event.name, data)
        ui.notify(f"Trusted client cert '{event.name}' saved", type="positive")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Upload failed: {exc}", type="negative")
    on_done()
