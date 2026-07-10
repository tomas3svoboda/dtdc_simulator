"""Tick loop worker thread (BuildSpec §8.1, §8.3).

`asyncua` runs on an asyncio event loop; integration is CPU-bound NumPy and
must not block it, so the tick loop runs in a dedicated worker thread and
talks to the model only through `RuntimeFacade.tick()`, which holds the lock
for its short critical sections.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from dtdc_simulator.engine.state_machine import SimState

if TYPE_CHECKING:
    from dtdc_simulator.engine.facade import RuntimeFacade

IDLE_POLL_S = 0.05


def run_forever(facade: "RuntimeFacade") -> None:
    while not facade.is_shutdown():
        if facade.state is SimState.RUNNING:
            facade.tick()
        else:
            time.sleep(IDLE_POLL_S)


def start_background_thread(facade: "RuntimeFacade") -> threading.Thread:
    t = threading.Thread(target=run_forever, args=(facade,), name="dtdc-tick-loop", daemon=True)
    t.start()
    return t
