# Myboxi-Hardware (Raspberry Pi 4)

Referenzaufbau für das Image aus `image/`. Andere Pins lassen sich über `/etc/myboxi-agent/myboxi-agent.env` einstellen (`MYBOXI_AGENT_PIN_*`).

## Einkaufsliste

| Teil | Hinweis |
|---|---|
| Raspberry Pi 4 (2 GB reichen) | |
| Netzteil USB-C 5,1 V / 3 A | Offizielles Pi-Netzteil oder Powerbank mit mindestens 3 A |
| microSD-Karte 32 GB, A1 | Markenware; der Stromausfall ist Normalbetrieb |
| NFC-Modul PN532 „V3“ | Per DIP-Schalter auf I2C gestellt |
| NFC-Tags NTAG213 oder NTAG215 | Aufkleber oder Münzen für die Figuren |
| I2S-Verstärker MAX98357A (Breakout) | Mono, 3 W |
| Lautsprecher 3 W, 4 Ω | 40–57 mm Durchmesser |
| 4 Taster | Arcade- oder Drucktaster, schließend |
| Dupont-Kabel (Buchse/Buchse) | Etwa 20 Stück |

## Verdrahtung

Pinnummern sind die physischen Pins der 40-poligen Leiste, dahinter die GPIO-Nummer (BCM).

| Bauteil | Anschluss | Pi-Pin |
|---|---|---|
| PN532 | VCC | 1 (3,3 V) |
| PN532 | GND | 6 (GND) |
| PN532 | SDA | 3 (GPIO2) |
| PN532 | SCL | 5 (GPIO3) |
| MAX98357A | VIN | 2 (5 V) |
| MAX98357A | GND | 9 (GND) |
| MAX98357A | BCLK | 12 (GPIO18) |
| MAX98357A | LRC | 35 (GPIO19) |
| MAX98357A | DIN | 40 (GPIO21) |
| Taster `play_pause` | ein Kontakt / anderer Kontakt | 11 (GPIO17) / 14 (GND) |
| Taster `volume_up` | | 13 (GPIO27) / GND |
| Taster `volume_down` | | 15 (GPIO22) / GND |
| Taster `next` | | 16 (GPIO23) / GND |

- Die Taster schalten gegen GND. Der Pi nutzt interne Pull-ups, Widerstände sind nicht nötig.
- **PN532:** DIP-Schalter auf I2C (bei den üblichen roten „V3“-Modulen: Schalter 1 = ON, 2 = OFF). Versorgung mit 3,3 V, nicht mit 5 V.
- **MAX98357A:** `SD` und `GAIN` offen lassen, das ergibt 9 dB Verstärkung und die Summe aus linkem und rechtem Kanal. Der Lautsprecher kommt an `+`/`−` des Moduls.
- Lautsprecher und NFC-Antenne nicht direkt übereinander, Metall schwächt das NFC-Feld.

## Konfiguration im Image

Das Image trägt in `/boot/firmware/config.txt` ein:

```text
dtparam=i2c_arm=on        # PN532
dtparam=i2s=on
dtoverlay=max98357a       # Verstärker als einziges Audiogerät
dtparam=audio=off
dtoverlay=vc4-kms-v3d,noaudio
```

## Prüfen

Mit SSH-Zugang (siehe `docs/box-inbetriebnahme.md`):

```text
sudo i2cdetect -y 1                       # PN532 erscheint als 24
sudo -u myboxi XDG_RUNTIME_DIR=/run/user/$(id -u myboxi) /opt/myboxi-agent/current/.venv/bin/myboxi-agent doctor
```

`doctor` prüft NFC-Leser, Audio, Netzwerk, Agent und Server und nennt, was fehlt.
