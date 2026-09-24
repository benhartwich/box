"""Agent runtime: wires store, adapters and core, runs the input, tick and control loops."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from myboxi_agent.adapters.base import Placed
from myboxi_agent.adapters.bundle import Adapters, sim_adapters
from myboxi_agent.adapters.outbox import EventOutbox, read_boot_id
from myboxi_agent.adapters.sim import SimButtons, SimPlayer, SimReader
from myboxi_agent.config import Settings
from myboxi_agent.control import ControlServer
from myboxi_agent.core.buttons import ButtonTracker
from myboxi_agent.core.controller import Controller
from myboxi_agent.store.db import connect
from myboxi_agent.store.repos import (
    AssetRepo,
    Database,
    LibraryRepo,
    OutboxRepo,
    ResumeRepo,
    StateRepo,
)

log = logging.getLogger(__name__)

TICK_S = 0.1
CONTROLLER_TICK_S = 1.0


def build_adapters(settings: Settings) -> Adapters:
    if settings.sim:
        return sim_adapters()
    from myboxi_agent.adapters.hardware import hardware_adapters

    return hardware_adapters(settings)


class App:
    def __init__(self, settings: Settings, adapters: Adapters | None = None) -> None:
        self.settings = settings
        self.adapters = adapters or build_adapters(settings)
        clock = self.adapters.clock
        self.db = Database(connect(settings.db_path), settings.asset_dir, clock)
        self.state = StateRepo(self.db)
        self.library = LibraryRepo(self.db)
        self.assets = AssetRepo(self.db)
        self.outbox_repo = OutboxRepo(self.db)
        self.outbox = EventOutbox(self.outbox_repo, clock, read_boot_id())
        self.announcer = self.adapters.announcer_factory(lambda: self.controller.prompt_volume())
        self.controller = Controller(
            clock=clock,
            player=self.adapters.player,
            announcer=self.announcer,
            outbox=self.outbox,
            resume_store=ResumeRepo(self.db),
            library=self.library,
            system=self.adapters.system,
            config=self.state.device_config,
            rng=random.Random(),
        )
        self.tracker = ButtonTracker(clock)
        player: Any = self.adapters.player
        if hasattr(player, "on_playlist_finished"):
            player.on_playlist_finished = self.controller.playlist_finished
            player.on_error = self.controller.player_error
        self.control = ControlServer(settings.control_socket, self.handle_control)
        self._stopping = asyncio.Event()

    # --- loops -------------------------------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "agent starting",
            extra={"device_id": str(self.state.get().device_id), "sim": self.settings.sim},
        )
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._reader_loop())
                tg.create_task(self._buttons_loop())
                tg.create_task(self._tick_loop())
                tg.create_task(self.control.serve())
                tg.create_task(self._stop_on_request())
                for background in self.adapters.background:
                    tg.create_task(background())
        except* _Stop:
            pass
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stopping.set()

    async def _stop_on_request(self) -> None:
        await self._stopping.wait()
        raise _Stop

    def shutdown(self) -> None:
        """Save the position on SIGTERM / power button (CLAUDE.md rule 5)."""
        self.controller.save_position()
        log.info("agent stopped")

    async def _reader_loop(self) -> None:
        async for event in self.adapters.reader.events():
            if isinstance(event, Placed):
                self.controller.token_placed(event.uid)
                session = self.controller.session
                if session is not None and session.playing:
                    self.library.mark_played(session.sources())
            else:
                self.controller.token_removed()

    async def _buttons_loop(self) -> None:
        async for event in self.adapters.buttons.events():
            actions = (
                self.tracker.press(event.button)
                if event.pressed
                else self.tracker.release(event.button)
            )
            for action in actions:
                self.controller.button(action)

    async def _tick_loop(self) -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(TICK_S)
            for action in self.tracker.tick():
                self.controller.button(action)
            elapsed += TICK_S
            if elapsed >= CONTROLLER_TICK_S:
                elapsed = 0.0
                self.controller.tick()

    # --- control socket ----------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        st = self.state.get()
        playback = self.controller.status()
        return {
            "ok": True,
            "device_id": str(st.device_id),
            "paired": st.tenant_id is not None,
            "server_url": st.server_url or self.settings.default_server_url,
            "applied_config_rev": st.applied_config_rev,
            "applied_device_rev": st.applied_device_rev,
            "playback": playback.status,
            "token_id": str(playback.token_id) if playback.token_id else None,
            "volume": playback.volume,
            "pairing_code": self.controller.pairing_code,
            "outbox": self.outbox_repo.count(),
            "sim": self.settings.sim,
        }

    async def handle_control(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "status":
            return self.status()
        a = self.adapters
        if not isinstance(a.reader, SimReader) or not isinstance(a.buttons, SimButtons):
            return {"ok": False, "error": "simulation commands need --sim"}
        match cmd:
            case "place":
                a.reader.place(str(req["uid"]).upper())
            case "remove":
                a.reader.remove()
            case "press":
                await a.buttons.hold([str(req["button"])], 0.05)
            case "hold":
                await a.buttons.hold([str(b) for b in req["buttons"]], float(req["seconds"]))
            case "finish":
                if isinstance(a.player, SimPlayer):
                    a.player.finish()
            case _:
                return {"ok": False, "error": f"unknown command {cmd!r}"}
        await asyncio.sleep(0.2)  # let the loops react before reporting
        return self.status()


class _Stop(Exception):
    pass
