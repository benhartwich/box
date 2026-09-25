"""Podcast feeds: RSS 2.0 and Atom, parsed incrementally with the stdlib expat (SPEC v0.8 §8.2).

Only what the box needs is kept: key, title, enclosure, date, duration and the explicit flag.
Entity declarations make a feed invalid (no entity expansion attacks); external entities are
never loaded. Size limits: 20 MB of XML (enforced by the caller while streaming) and 5000
entries.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import re
from dataclasses import dataclass
from typing import Any
from xml.parsers import expat

MAX_FEED_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_TEXT = 1024  # characters kept per field
TITLE_MAX = 200

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
AUDIO_TYPES = frozenset({"application/ogg"})


class FeedError(Exception):
    """The document is not a usable feed (``playback_error`` ``feed_error``)."""


@dataclass(frozen=True)
class Episode:
    key: str
    title: str
    url: str
    mime: str | None
    length: int | None
    published_at: dt.datetime | None
    duration_ms: int
    explicit: bool | None  # None: not stated, the channel's flag applies
    feed_order: int

    @property
    def is_audio(self) -> bool:
        return self.mime is None or self.mime.startswith("audio/") or self.mime in AUDIO_TYPES


@dataclass(frozen=True)
class Feed:
    episodes: list[Episode]
    explicit: bool  # channel level

    def eligible(self) -> list[Episode]:
        """Audio episodes that are not explicit, newest first; undated ones keep feed order
        after the dated ones (SPEC v0.8 §8.2)."""
        ok = [
            e
            for e in self.episodes
            if e.is_audio and not (self.explicit if e.explicit is None else e.explicit)
        ]
        epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
        return sorted(
            ok, key=lambda e: (e.published_at is not None, e.published_at or epoch, -e.feed_order),
            reverse=True,
        )  # fmt: skip


def episode_key(guid: str | None, url: str) -> str:
    """SPEC v0.8 §8.2: first 32 hex digits of SHA-256 over the guid, else the enclosure URL."""
    return hashlib.sha256((guid or url).encode()).hexdigest()[:32]


def parse_duration(text: str) -> int:
    """``itunes:duration``: seconds, ``MM:SS`` or ``HH:MM:SS`` → milliseconds (0 if unclear)."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        return 0
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0.0
    for v in values:
        if v < 0:
            return 0
        seconds = seconds * 60 + v
    return int(seconds * 1000) if seconds < 7 * 24 * 3600 else 0


def parse_date(text: str) -> dt.datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(text)  # RSS: RFC 822
    except (TypeError, ValueError, IndexError):
        try:
            parsed = dt.datetime.fromisoformat(text)  # Atom: RFC 3339
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def parse_explicit(text: str) -> bool | None:
    value = text.strip().lower()
    if value in ("yes", "true", "explicit"):
        return True
    if value in ("no", "false", "clean"):
        return False
    return None


def _length(text: str | None) -> int | None:
    if text and re.fullmatch(r"\d{1,15}", text.strip()):
        return int(text.strip()) or None
    return None


def _http_url(text: str | None) -> str | None:
    if not text:
        return None
    url = text.strip()
    if len(url) > 2048 or not re.match(r"^https?://\S+$", url, re.IGNORECASE):
        return None
    return url


class _Entry:
    def __init__(self) -> None:
        self.guid: str | None = None
        self.title = ""
        self.url: str | None = None
        self.mime: str | None = None
        self.length: int | None = None
        self.published: dt.datetime | None = None
        self.duration_ms = 0
        self.explicit: bool | None = None


class FeedParser:
    """Feed the XML in chunks, then ``close()`` returns the feed."""

    def __init__(self) -> None:
        self._parser = expat.ParserCreate(namespace_separator=" ")
        self._parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        self._parser.EntityDeclHandler = self._entity_decl
        self._parser.ExternalEntityRefHandler = self._external_entity
        self._parser.StartElementHandler = self._start
        self._parser.EndElementHandler = self._end
        self._parser.CharacterDataHandler = self._chars
        self._stack: list[str] = []
        self._entry: _Entry | None = None
        self._text: list[str] | None = None
        self._text_len = 0
        self._episodes: list[Episode] = []
        self._count = 0
        self._channel_explicit: bool | None = None
        self._root: str | None = None

    # --- security ---------------------------------------------------------------------------

    def _entity_decl(self, *args: Any) -> None:
        raise FeedError("entity declaration")

    def _external_entity(self, *args: Any) -> int:
        raise FeedError("external entity")

    # --- api --------------------------------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        try:
            self._parser.Parse(chunk, False)
        except expat.ExpatError as exc:
            raise FeedError(str(exc)) from exc

    def close(self) -> Feed:
        try:
            self._parser.Parse(b"", True)
        except expat.ExpatError as exc:
            raise FeedError(str(exc)) from exc
        if self._root not in ("rss", f"{ATOM} feed"):
            raise FeedError("not an RSS or Atom feed")
        return Feed(self._episodes, bool(self._channel_explicit))

    # --- handlers ---------------------------------------------------------------------------

    def _start(self, name: str, attrs: dict[str, str]) -> None:
        if self._root is None:
            self._root = name
        parent = self._stack[-1] if self._stack else None
        self._stack.append(name)
        if name in ("item", f"{ATOM} entry"):
            self._count += 1
            if self._count > MAX_ENTRIES:
                raise FeedError("too many entries")
            self._entry = _Entry()
            return
        entry = self._entry
        if entry is None:
            if name == f"{ITUNES} explicit" and parent == "channel":
                self._collect()
            return
        if name == "enclosure" and entry.url is None:
            self._enclosure(entry, attrs.get("url"), attrs.get("type"), attrs.get("length"))
        elif name == f"{ATOM} link" and attrs.get("rel") == "enclosure" and entry.url is None:
            self._enclosure(entry, attrs.get("href"), attrs.get("type"), attrs.get("length"))
        elif name in (
            "title", "guid", "pubDate", f"{ITUNES} duration", f"{ITUNES} explicit",
            f"{ATOM} title", f"{ATOM} id", f"{ATOM} published", f"{ATOM} updated",
        ):  # fmt: skip
            self._collect()

    def _enclosure(
        self, entry: _Entry, url: str | None, mime: str | None, length: str | None
    ) -> None:
        entry.url = _http_url(url)
        entry.mime = (mime or "").split(";")[0].strip().lower() or None
        entry.length = _length(length)

    def _collect(self) -> None:
        self._text, self._text_len = [], 0

    def _chars(self, data: str) -> None:
        if self._text is not None and self._text_len < MAX_TEXT:
            self._text.append(data[: MAX_TEXT - self._text_len])
            self._text_len += len(data)

    def _end(self, name: str) -> None:
        self._stack.pop()
        text = "".join(self._text).strip() if self._text is not None else None
        self._text = None
        entry = self._entry
        if entry is None:
            if name == f"{ITUNES} explicit" and text is not None:
                self._channel_explicit = parse_explicit(text)
            return
        if name in ("item", f"{ATOM} entry"):
            self._finish(entry)
            self._entry = None
            return
        if text is None:
            return
        if name in ("title", f"{ATOM} title"):
            entry.title = entry.title or text
        elif name in ("guid", f"{ATOM} id"):
            entry.guid = entry.guid or text or None
        elif name in ("pubDate", f"{ATOM} published"):
            entry.published = parse_date(text) or entry.published
        elif name == f"{ATOM} updated":
            entry.published = entry.published or parse_date(text)
        elif name == f"{ITUNES} duration":
            entry.duration_ms = parse_duration(text)
        elif name == f"{ITUNES} explicit":
            entry.explicit = parse_explicit(text)

    def _finish(self, entry: _Entry) -> None:
        if entry.url is None:
            return
        title = " ".join(entry.title.split())[:TITLE_MAX] or "Folge"
        self._episodes.append(
            Episode(
                key=episode_key(entry.guid, entry.url),
                title=title,
                url=entry.url,
                mime=entry.mime,
                length=entry.length,
                published_at=entry.published,
                duration_ms=entry.duration_ms,
                explicit=entry.explicit,
                feed_order=len(self._episodes),
            )
        )


def parse_feed(data: bytes) -> Feed:
    """Convenience for tests and small documents."""
    parser = FeedParser()
    parser.feed(data)
    return parser.close()
