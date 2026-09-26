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

## 6. Podcasts

1. In der Web-UI unter **Inhalte → Podcast** die Feed-Adresse eintragen (RSS oder Atom, `https://…`) und festlegen, wie viele der neuesten Folgen auf die Box sollen.
2. Den Podcast einer Figur zuordnen. Die Box liest den Feed selbst, gleich nach dem nächsten Abgleich, und lädt die Folgen vollständig herunter. Bis die erste Folge da ist, sagt sie „Wird noch geladen“.
3. Danach spielen die Folgen auch ohne Internet. Alle 6 Stunden sieht die Box nach neuen Folgen; ältere räumt sie weg, wenn der Platz knapp wird. Beim Auflegen geht es in der zuletzt gehörten Folge weiter, auch wenn inzwischen eine neue erschienen ist.

Gut zu wissen:
- Folgen, die der Feed als nicht jugendfrei markiert (`itunes:explicit`), spielt die Box nicht. Video-Podcasts lässt sie aus.
- Die Box gleicht die Lautheit an (−16 LUFS, wie bei hochgeladenen Dateien). Die Höchstlautstärke der Box gilt wie immer.
- Die Box lädt nur von öffentlichen Adressen, nicht aus dem Heimnetz.
- Feed und Folgen kommen direkt vom Anbieter des Podcasts, nicht über den Myboxi-Server. Der Anbieter sieht dabei die IP-Adresse eures Anschlusses.
- Lässt sich ein Feed nicht lesen, steht das auf der Box-Seite unter „Probleme bei der Wiedergabe“.

## 7. Radio

Unter **Inhalte → Radio** die Stream-Adresse eines Senders eintragen (`https://…`, auch `.m3u` oder `.pls`) und einer Figur zuordnen. Radio spielt nur mit Internet und immer live; die Weiter-Taste hat hier keinen nächsten Titel. Radio ist auf jeder Box eingeschaltet und lässt sich auf der Box-Seite unter **Quellen** abschalten.

## 8. Von vorn oder ab einem bestimmten Titel

Auf der Seite einer Figur zeigt **Weiterhören**, wo sie zuletzt war. Mit „Von vorn beginnen“ oder „Ab diesem Titel“ legt ihr fest, wo sie beim nächsten Auflegen startet. Das gilt einmal; danach spielt sie wieder dort weiter, wo das Kind aufgehört hat. Die Box übernimmt es beim nächsten Abgleich, spätestens nach 15 Minuten.

## 9. Spotify

Voraussetzungen:
- Spotify Premium.
- Ein eigener Spotify-Schlüssel: im [Spotify-Entwicklerportal](https://developer.spotify.com/dashboard) anmelden, „Spotify Soloist API Key“ öffnen und einen Schlüssel erzeugen. Er gehört zu eurem Konto und wird nicht weitergegeben.
- Die Spotify-Bedingungen erlauben nur private, nicht-kommerzielle Nutzung.

Einrichten:
1. Einrichtungsmodus starten (**`volume_up` + `volume_down` 5 Sekunden halten**). Auf der Einrichtungsseite unter „Spotify (optional)“ den Schlüssel einfügen, dazu wie gewohnt WLAN und Passwort. Die Seite zeigt den Schlüssel nie wieder an. Leer lassen behält ihn, „Schlüssel löschen“ entfernt ihn.
2. In der Web-UI auf der Box-Seite unter **Quellen** „Spotify“ einschalten.
3. Die Box lädt Spotify Soloist selbst von Spotify herunter (etwa 13 MB). Die Box-Seite zeigt „Spotify wird eingerichtet …“, danach „Noch kein Spotify-Konto verbunden“.
4. Einmal anmelden: Handy im selben WLAN, in der Spotify-App unten auf das Geräte-Symbol tippen und **Myboxi NNNN** wählen. Danach steht auf der Box-Seite „bereit“.
5. Unter **Inhalte → Spotify** ein Album oder eine Playlist anlegen und einer Figur zuordnen: entweder den Link aus der Spotify-App einfügen („Teilen → Link kopieren“) oder, bequemer, direkt suchen.

Suchen in der App: Unter **Inhalte → Spotify → „Spotify verbinden“** führt die App einmal durch die Einrichtung einer eigenen Spotify-App im [Spotify-Entwicklerportal](https://developer.spotify.com/dashboard), mit Premium-Konto, Redirect URI `https://app.myboxi.eu/spotify/callback`, Häkchen bei „Web API“. Danach die Client-ID eintragen und verbinden. Dann gibt es eine Suche, „Meine Playlists“ und „Meine Alben“; Titel und Cover kommen von Spotify. Das geht mit demselben Entwicklerkonto wie der Spotify-Schlüssel der Box.

So verhält sich die Box:
- Figur auflegen: Das Album spielt, beim nächsten Mal an derselben Stelle weiter. Die Weiter-Taste springt zum nächsten Titel.
- Ist das Album zu Ende, hält die Box an. Titel, die Spotify von selbst anhängt (Autoplay), spielt sie nicht.
- Titel mit Explicit-Kennzeichnung überspringt die Box, außer ihr erlaubt sie auf der Box-Seite.
- Aus der Spotify-App lässt sich die Box wie ein Lautsprecher nutzen. Höchstlautstärke, Ruhezeiten und Einschlaf-Timer gelten trotzdem. Legt jemand eine Figur auf, hat die Figur Vorrang.
- Ohne Internet spielt Spotify nicht; die Box sagt „Das geht gerade leider nicht“.
- Spotify-Versionen laufen nach 90 Tagen ab. Die Box holt sich rechtzeitig eine neue, aber nie während Spotify spielt.

Im Heimnetz: Damit die Spotify-App die Box findet, ist sie bei eingeschaltetem Spotify im Heimnetz sichtbar. Aus dem Internet ist die Box nicht erreichbar; ihre Firewall lässt nur Verbindungen aus dem Heimnetz zu.

## Updates

Ab Image 0.3.0 aktualisiert sich die Box selbst, sobald sie online ist, aber nie während etwas spielt. Den Stand zeigt die App auf der Box-Seite unter „Software“; dort lassen sich automatische Updates auch abschalten. Sicherheitsupdates des Systems kommen täglich, ein nötiger Neustart erst nach 10 ruhigen Minuten. Boxen mit einem älteren Image brauchen einmal das neue Image. Details: `docs/updates.md`.

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
sudo journalctl _SYSTEMD_USER_UNIT=myboxi-soloist.service -f    # Spotify
```

Die mitgelieferten Ansagen spricht die Stimme „Thorsten-Voice/Kokoro“ (Apache-2.0, Lizenzhinweis in `/opt/myboxi-agent/current/prompts/NOTICE.txt`).

Eigene Ansagen, z. B. mit deiner Stimme: Opus-Dateien mit dem Namen der Ansage (siehe `agent/prompts.toml`, z. B. `unknown_token.opus`) nach `/var/lib/myboxi/prompts/` kopieren (Besitzer `myboxi`). Sie haben Vorrang vor den mitgelieferten.
