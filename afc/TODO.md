# AFC NFC Integration — TODO / Future Ideas

## LED Enhancements

- **Breathing effect for low spool using led_effect plugin** — Currently low
  spool lanes dim to 20%. With the `led_effect` Klipper plugin, we could do a
  true pulsing/breathing animation per lane. Would need separate effect
  definitions for each lane since led_effect operates on the full chain by
  default. Example config:
  ```ini
  [led_effect lane_breathe]
  leds:
      bt_leds
  autostart: false
  frame_rate: 24
  layers:
      breathing  2  0  top  (0.0, 0.0, 0.0)
  ```
  Then in the macro, replace the dimmed SET_LED with:
  ```
  SET_LED LED=bt_leds RED={r} GREEN={g} BLUE={b} INDEX={led_index}
  SET_LED_EFFECT EFFECT=lane_breathe
  ```
  And add `STOP_LED_EFFECTS` in the non-breathing path.

## Hardware

- **ESP32 mount** — need a bracket or enclosure for the ESP32 inside the
  BoxTurtle, possibly under one of the trays
- **PN532 tray redesign** — current STL is a hack, needs a proper parametric
  design with better PN532 retention and cable management

## Middleware

- **Lane ejection via Moonraker websocket** — alternative to file watcher,
  subscribe to AFC's Moonraker object namespace for real-time lane state
- **MQTT auto-reconnect** — add `on_disconnect` callback for automatic
  reconnection after broker drops
- **Deprecated MQTT client API** — `mqtt.Client()` without CallbackAPIVersion
  will break in a future paho-mqtt release

## Integration

- **Feature request to AFC/ArmoredTurtle** — ask about native Spoolman
  filament color support for `led_ready` and `led_tool_loaded` states, which
  would eliminate the need for the LED override macro entirely
- **AFC + klipper-toolchanger** — research how to run AFC-Klipper alongside
  klipper-toolchanger on a MadMax setup without filament sensors
