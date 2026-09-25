# Myboxi in Betrieb nehmen

Vom Image bis zur ersten Figur. Hardware und Verdrahtung: `docs/hardware.md`.

**Am einfachsten mit dem Einrichtungs-Assistenten der Web-UI:** Haushalt anlegen (oder auf der Startseite „Erste Box einrichten“), dann führt er durch alle Schritte unten. Nach dem Koppeln zeigt er live, was die Box meldet: Selbsttest (NFC-Leser, Lautsprecher, Taster, Ansagen), Tastentest, erste Figur, Inhalt, Laden und Abspielen. Bei Problemen nennt er, was zu prüfen ist. Diese Anleitung ist die Referenz dazu.

## 1. Image flashen

1. Das neueste Image herunterladen: GitHub → Releases → `myboxi-<version>.img.xz`. Zwischenstände aus Branches liegen unter Actions → „image“ → Artefakt `myboxi-image`: eine ZIP-Datei, die du zuerst entpackst.
2. Raspberry Pi Imager (Version 2) → Gerät „Raspberry Pi 4“ → Betriebssystem „Eigenes Image verwenden“ → die `.img.xz` wählen → SD-Karte → Schreiben.
3. Die Einstellungen des Imagers sind optional:
   - **WLAN** kannst du hier schon eintragen, dann entfällt Schritt 3.
   - Einen **SSH-Schlüssel** brauchst du nur zur Fehlersuche.
   - **Hostname** und **WLAN-Land** setzt die Box sonst selbst: `myboxi-NNNN`, Land AT.

## 2. Einschalten

SD-Karte einlegen und Netzteil anstecken. Der erste Start dauert etwa zwei Minuten, weil die Karte vergrößert wird.

## 3. WLAN einrichten (Einrichtungsmodus)

Ohne WLAN sagt die Box: „Einrichtung. Verbinde dein Handy mit dem WLAN Myboxi, Nummer …“.

1. Mit dem Handy das offene WLAN `Myboxi-NNNN` wählen. Die Einrichtungsseite öffnet sich von selbst, sonst http://10.42.0.1/ aufrufen.
2. Heim-WLAN wählen, Passwort eingeben. Den Server nur ändern, wenn du einen eigenen betreibst (Vorgabe `https://app.myboxi.eu`).
3. „Verbinden“ tippen. Die Box sagt an, ob es geklappt hat. Bei einem Fehler öffnet sie das Einrichtungs-WLAN erneut.

Später lässt sich der Einrichtungsmodus jederzeit starten: **`volume_up` + `volume_down` 5 Sekunden halten.** Nach 15 Minuten ohne Eingabe endet er von selbst.

## 4. Box koppeln

Sobald die Box online ist, sagt sie ihren Kopplungscode an: „Dein Code ist: vier – sieben – …“. Wiederholen: **`play_pause`** drücken. Der Code gilt 10 Minuten, danach kommt ein neuer.

In der Web-UI (https://app.myboxi.eu) → **Boxen** → „Box hinzufügen“ → Code und Name eingeben. Die Box bestätigt mit „Geschafft!“ und lädt sofort alle Inhalte.

In der ersten Stunde nach dem Koppeln (Einrichtungsphase, SPEC §9.6) piept die Box bei jedem Tastendruck und meldet die gedrückten Tasten an den Assistenten; sie gleicht sich alle 30 Sekunden mit dem Server ab.

Neu koppeln (z. B. für einen anderen Haushalt): **`play_pause` + `next` 5 Sekunden halten.**

## 5. Figuren

1. Eine noch unbekannte Figur auflegen. Die Box sagt „Diese Figur kenne ich noch nicht“.
2. In der Web-UI unter **Figuren → Unbekannte Figuren** mit „Übernehmen“ anlegen, einen Inhalt zuordnen.
3. Nach einer unbekannten Figur fragt die Box 10 Minuten lang alle 30 Sekunden beim Server nach, sonst alle 15 Minuten. Sie lädt die Dateien vollständig. Danach spielt die Figur auch ohne Internet. Die Figur einfach noch einmal auflegen.

## Checkliste für die erste Inbetriebnahme

- [ ] Einrichtungs-WLAN `Myboxi-NNNN` erscheint, die Seite öffnet sich
- [ ] Nach dem Verbinden: Ansage „Die Box ist mit dem WLAN verbunden“
- [ ] Kopplungscode wird angesagt, Box erscheint in der Web-UI
- [ ] Unbekannte Figur: Ansage, Figur taucht unter „Unbekannte Figuren“ auf
- [ ] Figur mit Inhalt: Start-Ton, Wiedergabe über den Lautsprecher
- [ ] Tasten: Pause/Weiter, lauter/leiser, nächster Titel; nie lauter als die Höchstlautstärke der Box
- [ ] Figur abnehmen und wieder auflegen: spielt an der gleichen Stelle weiter
- [ ] Stecker ziehen, wieder einstecken, Figur auflegen: spielt an der gleichen Stelle weiter
- [ ] In der Web-UI unter Boxen: „zuletzt gemeldet“ und Wiedergabestatus aktuell

## Fehlersuche

Mit SSH (Schlüssel über den Imager gesetzt):

```text
sudo -u myboxi XDG_RUNTIME_DIR=/run/user/$(id -u myboxi) /opt/myboxi-agent/current/.venv/bin/myboxi-agent doctor
sudo journalctl _SYSTEMD_USER_UNIT=myboxi-agent.service -f      # Agent
sudo journalctl -u myboxi-setupd -f                             # Einrichtungsmodus
```

Die mitgelieferten Ansagen spricht die Stimme „Thorsten-Voice/Kokoro“ (Apache-2.0, Lizenzhinweis in `/opt/myboxi-agent/current/prompts/NOTICE.txt`).

Eigene Ansagen, z. B. mit deiner Stimme: Opus-Dateien mit dem Namen der Ansage (siehe `agent/prompts.toml`, z. B. `unknown_token.opus`) nach `/var/lib/myboxi/prompts/` kopieren (Besitzer `myboxi`). Sie haben Vorrang vor den mitgelieferten.
