# Future Enhancements

Ideas and known improvements for future development. Contributions welcome.

---

## Middleware

**Smarter Spoolman lookups**
`find_spool_by_nfc` currently fetches every spool from Spoolman on every scan and loops through them in Python to find a match. Spoolman's API supports filtering — we should query directly by NFC ID instead of pulling the whole list. Not a problem at 10 spools, starts to feel sloppy at 50+.

**MQTT auto-reconnect**
The middleware has no reconnect logic. If the MQTT broker goes down (Home Assistant update, power blip, whatever), the script dies and stays dead until you manually restart it. Needs an `on_disconnect` callback with automatic reconnect so it just heals itself.

**Configurable low spool threshold**
The 100g low spool warning is hardcoded. Should be a variable at the top of the config alongside the other settings — people running 250g mini spools have very different needs than someone running 3kg spools.

---

## ESPHome

**Fix scan flash vs. MQTT publish order**
Right now the white flash plays out fully before the MQTT publish fires. That means there's a window where the flash is done but the LED hasn't updated to the spool color yet. The publish should fire immediately when the tag is scanned, with the flash happening in parallel while the middleware does its work.

**Remove dead lambda in color handler**
There's a leftover no-op lambda at the top of the color MQTT handler that does nothing. Just noise — should be cleaned up.

**`on_tag_removed` handling**
The PN532 supports an `on_tag_removed` event that fires when a tag leaves the reader. Right now the LED holds the last color indefinitely after you pull the spool away. Could dim the LED or turn it off when the tag is removed to make it clearer nothing is actively on the reader.

**Single shared base config**
All 4 ESPHome YAML files are nearly identical — the only real differences are the toolhead name, static IP, and topic names. Any change to shared logic (like the LED effects we just added) has to be copy-pasted across all 4 files. ESPHome supports `!include` and packages — a single `base.yaml` with all the logic, and each toolhead file just defines its name and IP, would make maintenance much cleaner.

---

## Home Assistant Integration

**Push notifications for low spool and unknown tags**
The middleware already knows when a spool is low or when an unknown tag is scanned — it's just logging it to the console. It could publish to Home Assistant's notification service at the same time so you get a phone alert rather than relying on noticing the LED.

---
