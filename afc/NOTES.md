# Ideas for Writing Updates on Filament Usage to an OpenPrintTag

## The Write Logic to OpenPrintTag
Function to be triggered **60 seconds** after a filament unload is detected. This delay ensures that the final usage data is correctly recorded in Spoolman before being written back to the physical tag.

```python
def write_usage_to_tag(lane_id, spool_id):
    """Fetch current usage from Spoolman and write to NFC tag."""
    try:
        # 1. Get the latest 'consumed_weight' from Spoolman
        resp = requests.get(f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}")
        if resp.status_code == 200:
            spool = resp.json()
            usage = spool.get("consumed_weight", 0)
            
            # 2. Determine which reader handles this lane
            reader_id = "reader1" if lane_id in [1, 2] else "reader2"
            
            # 3. Construct the OpenPrintTag payload
            # Key 0 is standard for 'consumed_weight' in many OpenPrintTag implementations
            payload = {0: round(usage, 2)}
            hex_data = encode_openprinttag(payload)
            
            if hex_data:
                # 4. Publish to the ESPHome write topic
                topic = f"nfc/{reader_id}/write"
                client.publish(topic, hex_data)
                logging.info(f"Wrote {usage}g to tag on Lane {lane_id} via {reader_id}")
    except Exception as e:
        logging.error(f"Failed to write usage to tag: {e}")
```

## CBOR Encoding
OpenPrintTag uses the **CBOR** format for data storage. Use the `cbor2` library to convert a Python dictionary into the binary format required by the tag.

```python
def encode_openprinttag(data_dict):
    """Encode dictionary to CBOR hex string."""
    try:
        # Convert dict to CBOR binary, then to hex string for MQTT transit
        encoded = cbor2.dumps(data_dict)
        return encoded.hex()
    except Exception as e:
        logging.error(f"CBOR Error: {e}")
        return None
```

## ESP32 Firmware (ESPHome)
On the ESP32 side, the reader listens for the `nfc/readerX/write` topic and executes a C lambda to perform the physical write to the tag's NDEF area.

```yaml
on_message:
  - topic: nfc/reader1/write
    then:
      - lambda: |-
          // Converts the hex string back to bytes and writes to the tag's NDEF area
          std::vector<uint8_t> data = hex_to_bytes(x);
          id(reader1).write_ndef(data);
```

---

## 👻 Possible Problems I Foresee
Using a single **PN5180** to read two lanes introduces a significant risk: there will almost certainly be times when the system tries to write an update, but the scanner picks up the **wrong NFC tag** because both are in the field.

I might be tempted to just use a manual macro with a 3rd dedicated "update station" PN5180;....but I think I can implement a **Targeted Write** to solve this programmatically.
I can always fallback to the manual method if this doesn't work out

### Targeted Write Solution

#### Python Side
When the 1-minute timer expires, the script looks up the `target_uid` that was originally assigned to the lane. It sends a JSON message containing both that **UID** and the **NDEF data**.

#### ESP32 Side
The ESP32 firmware parses the JSON and checks if the `target_uid` is actually present in the reader's field.
*   **Match Found:** It performs the write.
*   **No Match:** (e.g., the tag was swapped during the 60-second window) It aborts the write and logs a warning to prevent data corruption.

This is something I actually have already been pondering for loading filament and having mismatches.

If Tag Usage > Spoolman Usage: The spool was likely used elsewhere. We update Spoolman to match the tag so you don't run out of filament mid-print.

If Tag Usage < Spoolman Usage: Same issue as above. Log a warning but stick with Spoolman's higher number

