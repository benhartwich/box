"""``core.ports.Player`` over two backends: mpv for files, Soloist for Spotify (SPEC v0.9 §8.1).

Exactly one backend is active. Starting on one stops the other, so a figure never plays
over a Spotify session. The volume goes to both, so switching never jumps above the limit.
Callbacks from a backend reach the controller only while it is the active one; actions from
the Spotify app make Soloist active (the latest action wins).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from myboxi_agent.core.model import PlanItem, ResumePoint
from myboxi_protocol.state import RepeatMode


class PlaylistBackend(Protocol):
    on_playlist_finished: Callable[[], None] | None
    on_error: Callable[[str], None] | None

    def play(
        self, items: Sequence[PlanItem], index: int, position_ms: int, repeat: RepeatMode
    ) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def position(self) -> ResumePoint | None: ...


class ContextBackend(Protocol):
    on_playlist_finished: Callable[[], None] | None
    on_error: Callable[[str], None] | None
    on_remote_playing: Callable[[bool], None] | None
    on_external_started: Callable[[], None] | None
    on_external_volume: Callable[[int], None] | None

    def play_context(
        self, uri: str, start: ResumePoint, shuffle: bool, repeat: RepeatMode
    ) -> None: ...
    def skip(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def position(self) -> ResumePoint | None: ...


class RoutingPlayer:
    def __init__(self, local: PlaylistBackend, spotify: ContextBackend | None) -> None:
        self.local = local
        self.spotify = spotify
        self.active: PlaylistBackend | ContextBackend = local
        self.on_playlist_finished: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_remote_playing: Callable[[bool], None] | None = None
        self.on_external_started: Callable[[], None] | None = None
        self.on_external_volume: Callable[[int], None] | None = None
        local.on_playlist_finished = lambda: self._from(local, self.on_playlist_finished)
        local.on_error = lambda code: self._error_from(local, code)
        if spotify is not None:
            spotify.on_playlist_finished = lambda: self._from(spotify, self.on_playlist_finished)
            spotify.on_error = lambda code: self._error_from(spotify, code)
            spotify.on_remote_playing = self._remote_playing
            spotify.on_external_started = self._external_started
            spotify.on_external_volume = self._external_volume

    # --- core.ports.Player ------------------------------------------------------------------

    def play(
        self, items: Sequence[PlanItem], index: int, position_ms: int, repeat: RepeatMode
    ) -> None:
        if self.spotify is not None and self.active is self.spotify:
            self.spotify.stop()
        self.active = self.local
        self.local.play(items, index, position_ms, repeat)

    def play_context(self, uri: str, start: ResumePoint, shuffle: bool, repeat: RepeatMode) -> None:
        if self.spotify is None:
            if self.on_error is not None:
                self.on_error("not_configured")
            return
        if self.active is self.local:
            self.local.stop()
        self.active = self.spotify
        self.spotify.play_context(uri, start, shuffle, repeat)

    def skip(self) -> None:
        if self.spotify is not None and self.active is self.spotify:
            self.spotify.skip()

    def pause(self) -> None:
        self.active.pause()

    def resume(self) -> None:
        self.active.resume()

    def stop(self) -> None:
        self.active.stop()

    def set_volume(self, volume: int) -> None:
        self.local.set_volume(volume)
        if self.spotify is not None:
            self.spotify.set_volume(volume)

    def position(self) -> ResumePoint | None:
        return self.active.position()

    # --- callbacks --------------------------------------------------------------------------

    def _from(self, backend: object, callback: Callable[[], None] | None) -> None:
        if backend is self.active and callback is not None:
            callback()

    def _error_from(self, backend: object, code: str) -> None:
        if backend is self.active and self.on_error is not None:
            self.on_error(code)

    def _remote_playing(self, playing: bool) -> None:
        """Play or pause in the Spotify app. The controller first pauses what the box plays
        (still the active backend), then Soloist becomes active."""
        if (playing or self.active is self.spotify) and self.on_remote_playing is not None:
            self.on_remote_playing(playing)
        if playing and self.spotify is not None:
            self.active = self.spotify

    def _external_started(self) -> None:
        if self.on_external_started is not None:
            self.on_external_started()
        if self.spotify is not None:
            self.active = self.spotify

    def _external_volume(self, volume: int) -> None:
        if self.on_external_volume is not None:
            self.on_external_volume(volume)
