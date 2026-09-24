# box-protocol

Pydantic-v2-Modelle für das Box-Protokoll `v1` (SPEC v0.3, §5–§7). Genutzt von Agent und Server.
Änderungen hier sind immer auch Spec-Änderungen (CLAUDE.md, Regel 9).

Angelegt von der Server-Session (M1) nach SPEC v0.3. Danach geht der Besitz an die Agent-Session über;
jede weitere Änderung ist ein Spec-Änderungsvorschlag und braucht Freigabe.

Offene Punkte für die Agent-Session:
- `reported.playback.status` ist hier `stopped | playing | paused`. Die Spec nennt nur `playing`.
- `podcast.source.order` erlaubt `newest_first | oldest_first`. Die Spec nennt nur `newest_first`.
- `battery`, `image_version`, `wifi_rssi` und `soloist` in `reported` sind optional, denn nicht jede Box hat eine Akkuanzeige oder WLAN.
