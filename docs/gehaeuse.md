# Gehäuse: Box gestalten, drucken, anfragen

Das Gehäuse der Myboxi kommt aus dem 3D-Drucker. Unter **app.myboxi.eu/gestalten** wählt man:
- eine Form: Radio, Würfel, Bär, Einhorn, Katze, Hase, Frosch
- einen Namen und drei Farben
- das Lautsprechergitter

Danach sieht man die Box in 3D und lädt die Druckdateien herunter. Der Generator steckt in `hardware/case` (Paket `myboxi-case`). Er läuft auch ohne Server:

```bash
uv run myboxi-case build form=unicorn name=Emma color_front=flieder --out emma.zip
uv run myboxi-case render form=bear name=Mia --out mia.png
uv run myboxi-case check --all
```

Lizenzen:
- Druckdateien: CC BY-SA 4.0
- Generator: GPL-3.0-or-later

## Teile

| Teil | Druckrichtung | Hinweis |
|---|---|---|
| Korpus | Oberseite auf dem Bett | Deckel, Seiten, Rückwand, Rahmen für den NFC-Leser, Schienen für die Front |
| Front | Sichtseite auf dem Bett | Lautsprechergitter, Name, bei Tierfiguren das Gesicht |
| Boden | liegend | Abstandshalter für den Pi, Halter für den Verstärker, zwei Kabelbinder-Halter, bei Bedarf Powerbank-Fach mit Schlitzen für ein Klettband |
| Lautsprecherring | liegend | klemmt den Lautsprecher an die Front |
| Ohren, Augen, Horn | liegend, Horn stehend | nur bei Tierfiguren |
| Figurensockel | liegend | Druckpause bei 2,2 mm, NFC-Tag einlegen |

Alles druckt ohne Stützmaterial. Jedes Teil passt auf 180 × 180 mm, also auch auf kleine Drucker.

## Drucken

**Allgemein**
- PETG, 0,2 mm Schichthöhe, 4 Wände, 15 % Füllung.
- PLA geht mit dem Pi Zero 2 W auch. Der Pi 4 wird warm, dafür PETG nehmen.

**Mehrfarbig (Snapmaker U1, Orca, Bambu Studio)**
- Die 3MF-Dateien ordnen jedes Teil einem Kopf zu:

  | Kopf | Teile |
  |---|---|
  | 1 | Korpus, Boden, Ohren |
  | 2 | Front, Lautsprecherring |
  | 3 | Einlagen (Name, Tastensymbole, Figurenring, Gesicht), Horn und Figurensockel |

- Die Teile sind für eine 270-mm-Platte angeordnet. Die Ecke links hinten bleibt frei für den Reinigungsturm des U1.
- Passt nicht alles auf eine Platte, gibt es mehrere Dateien (`…-platte-1.3mf`, `…-platte-2.3mf`), etwa beim Radio.

**Einfarbig:** Im Konfigurator „Druck: Einfarbig“ wählen. Name und Symbole sind dann nur eingraviert. In einer mehrfarbigen Datei auf einem Einzeldüsen-Drucker die Teile „… Einlage“ löschen.

Die ZIP-Datei enthält eine `LIESMICH.txt` mit Stückliste und Montage.

## Stückliste

| Teil | Hinweis |
|---|---|
| Raspberry Pi Zero 2 W oder Pi 4 | Zero 2 W mit angelöteter Stiftleiste |
| NFC-Modul PN532 V3 | 42,7 × 40,4 mm, auf I2C gestellt |
| Verstärker MAX98357A | klemmt im Halter auf dem Boden, Platine bis 20 mm breit |
| Lautsprecher 40 mm, 3 W, 4 Ω | Radio: auch 50 oder 57 mm |
| 4 Taster 16 mm | Radio: auch 24-mm-Arcade-Taster |
| USB-C-Einbaubuchse mit Kabel | Ausschnitt 13 × 7 mm, Schrauben im Abstand von 24 mm |
| Schrauben | 4 × M3 × 10 (Boden), 3 × M2,5 × 6 (Lautsprecherring), 4 × M2,5 × 6 (Pi); Einhorn: 1 × M3 × 10 für das Horn |
| NFC-Tags | NTAG213 oder NTAG215, 25 mm, für die Figurensockel |

Verkabelung: `docs/hardware.md`.

## Maße vor dem Druck prüfen

Diese Teile gibt es in vielen Varianten. Der Generator geht von den folgenden Maßen aus (`hardware/case/src/myboxi_case/components.py`):

| Teil | Annahme |
|---|---|
| Taster 16 mm | Loch 16,2 mm, Mutter und Schlüssel brauchen Ø 24 mm, Körper 30 mm tief |
| USB-C-Buchse | Stecker bis 12,4 × 6,6 mm, Schrauben ± 12 mm von der Mitte |
| Lautsprecher | Rand 3 mm dick, Korb bis 22 mm tief |
| Powerbank | 93 × 61 × 23 mm |

**Spiel für Passungen** (Vorgabe 0,2 mm) passt für die meisten Drucker. Sitzt die Front zu stramm in den Schienen, 0,25 oder 0,3 wählen.

## Zusammenbau

Alles wird geschraubt oder gesteckt, nichts geklebt (außer Ohren und Augen der Tierfiguren).

1. Lautsprecher in die Front legen, Lautsprecherring aufschrauben.
2. NFC-Modul von unten in den Rahmen unter der Figurenmarke drücken. Die Lippen rasten ein.
3. Taster oben einsetzen und verschrauben, USB-C-Buchse hinten einschrauben.
4. Tierfiguren:
   - Ohren oder Augen in die Schlitze kleben.
   - Beim Einhorn das Horn von innen mit M3 × 10 festschrauben.
5. Pi auf den Boden schrauben. Den Verstärker von oben in seinen Halter schieben: Die Platine läuft in zwei Schlitzen und klemmt.
6. Verkabeln (`docs/hardware.md`). Die Kabel mit kleinen Kabelbindern (bis 3,6 mm) an den beiden Haltern auf dem Boden bündeln, damit die Front beim Einschieben nichts einklemmt. Mit Powerbank: diese mit einem Klettband (bis 20 mm) durch die zwei Schlitze im Boden festzurren.
7. Front von unten in die Schienen hinter dem Fenster schieben.
8. Boden einsetzen und mit 4 Schrauben M3 festschrauben. Die Front lässt sich nur bei offenem Boden herausnehmen; Kinder kommen nicht an die Elektronik.

**Ohne Löten**
- Raspberry Pi Zero 2 W mit vorgelöteter Stiftleiste („Zero 2 WH“).
- Taster mit Anschlusslitzen.
- Dupont-Kabel Buchse/Buchse, 20 cm.
- PN532-Module gibt es mit eingelöteter Stiftleiste; oft liegt sie aber lose bei.
- Der Lautsprecher kommt an die Schraubklemme des Verstärkers.

## Automatische Prüfungen

`myboxi-case check --all` baut jede erlaubte Kombination und prüft sie. Die Tests in CI tun dasselbe.

**Druckbarkeit**
- Jedes Teil ist ein geschlossener Körper und liegt flach auf dem Bett.
- Keine Stelle braucht Stützmaterial: Überhänge sind höchstens 44° steil, Brücken höchstens 16 mm lang.
- Keine Wand ist dünner als 0,8 mm.

**Bauteile**
- Jedes Bauteil hat mindestens 0,5 mm Abstand zu Teilen, an denen es nicht befestigt ist, und steckt nirgends im Kunststoff.
- Der Weg für den USB-C-Stecker ist frei.
- Das NFC-Feld unter der Figur ist frei von Metall und Lautsprecher; die Antenne liegt höchstens 4 mm unter der Oberfläche.

**Lautsprechergitter**
- Mindestens 30 % der Fläche sind offen.
- Kein Loch ist breiter als 5 mm, damit Finger und Stifte draußen bleiben.

Die 3MF-Dateien wurden mit Snapmaker Orca 2.4.0 und dem U1-Profil (0,20 mm Standard, PETG) gesliced: ohne Fehler, mit den Köpfen wie oben.

**Nicht automatisch prüfbar:** der echte Druck. Beim ersten Testdruck prüfen:
- Front in den Schienen
- Taster, USB-C-Buchse und Lautsprecher
- Leseweite des NFC-Lesers durch den Deckel mit Figur und Sockel
- Klang durch das Gitter

## Anfragen für gedruckte Gehäuse

Wer keinen Drucker hat, kann ein gedrucktes Gehäuse anfragen (ohne Elektronik). Die Funktion ist aus, bis `MYBOXI_SERVER_ORDER_NOTIFY_EMAIL` gesetzt ist (`docs/betrieb-debian13.md`).

**Ablauf**
1. Die anfragende Person füllt das Formular aus: Name, E-Mail, Land, Anzahl, Nachricht, Einwilligung.
2. Sie bekommt eine Mail mit einem Bestätigungslink, 48 Stunden gültig. Der Link zeigt nur einen Knopf; erst der Klick bestätigt, damit Mail-Scanner nichts auslösen.
3. Nach der Bestätigung gehen zwei Mails raus:
   - an den Betreiber: alle Angaben, Link zur Gestaltung und zu den Druckdateien; „Antworten“ geht direkt an die anfragende Person
   - an die anfragende Person: Eingangsbestätigung
4. Der Betreiber schickt selbst ein Angebot. Erst dessen Annahme ist eine Bestellung.

**Verwaltung:** `myboxi-server case-requests list [--status confirmed]`, `show <id>`, `status <id> answered|done|cancelled`, `delete <id>`.

**Aufbewahrung:** Unbestätigte Anfragen werden nach 48 Stunden gelöscht, alle anderen 12 Monate nach der letzten Änderung. Details in der Datenschutzerklärung von myboxi.eu.

**Missbrauchsschutz**
- Unsichtbares Feld gegen Bots.
- Höchstens 5 Anfragen pro Stunde und IP-Adresse, 3 pro Tag und E-Mail-Adresse.

**Vor dem ersten Verkauf klären** (nicht Teil der Software):
- Produktsicherheit eines Gehäuses für ein Kinderprodukt (Kleinteile, Material).
- Angebot und AGB: Bei personalisierten Waren entfällt das Rücktrittsrecht (FAGG § 18).
- Umsatzsteuer, Versand.
