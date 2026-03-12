# AFC Hardware Approaches

Two hardware approaches are supported for BoxTurtle lane scanning. Both use PN532 NFC modules and communicate via MQTT over ESPHome — they differ in how many ESP32s are used.

## Approach 1: Per-Lane (Current Default)

**One ESP32-S3-Zero + PN532 per lane.** This is the same hardware used by the toolchanger setup, making it easy to source parts and reuse ESPHome config patterns.

- **Config:** `integrations/afc/esphome/lane-pn532.yaml`
- **Hardware per lane:** 1x Waveshare ESP32-S3-Zero, 1x PN532 (I2C)
- **Hardware total (4 lanes):** 4x ESP32-S3-Zero, 4x PN532
- **Wiring:** Simple point-to-point I2C per board (GPIO1/SDA, GPIO2/SCL, 3V3, GND)
- **Power:** Each board powered independently — USB, or 3.3V/5V from AFC-Lite rail

**Advantages:**
- Same hardware as toolchanger setup — one part to stock
- Simple wiring — each board is independent
- Easier to debug — boards are isolated from each other
- Lock/clear per lane handled entirely on each ESP32

**Disadvantages:**
- More boards (4 instead of 1)
- More USB/power connections to manage

---

## Approach 2: 4-Reader Single ESP32 (Legacy)

**One ESP32-WROOM DevKit drives all 4 PN532 modules**, each on its own I2C bus. This was the original approach.

- **Config:** `integrations/afc/esphome/boxturtle-nfc.yaml`
- **Hardware:** 1x ESP32-WROOM DevKit, 4x PN532 (I2C)
- **Wiring:** 4 separate I2C buses (2 hardware, 2 software) — more complex wiring
- **Power:** All boards powered from AFC-Lite 5V rail via distribution point

**Advantages:**
- Single ESP32 to manage
- Single OTA update covers all lanes
- Fewer USB connections

**Disadvantages:**
- More complex wiring — each PN532 needs its own SDA/SCL pair routed to the ESP32
- Software I2C on lanes 3–4 is less reliable than hardware I2C
- WROVER modules (with PSRAM) lose GPIO16/17 — requires pin remapping
- Harder to isolate a fault to one lane
- Different hardware than toolchanger setup

See [wiring.md](wiring.md) for the detailed pin assignments for this approach.

---

## Which Should I Use?

**Start with the per-lane approach** (`lane-pn532.yaml`) unless you have a specific reason to prefer a single ESP32. It's simpler to wire, easier to debug, and uses the same hardware as the toolchanger setup.

The 4-reader approach (`boxturtle-nfc.yaml`) is kept for reference and for users who have already built it or prefer fewer boards.
