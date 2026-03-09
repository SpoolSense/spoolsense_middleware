The Write Logic
Function to be triggered 60 seconds after a filament unload is detected.

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


CBOR Encoding
Uses the cbor2 library to convert the Python dictionary into the compact binary format required by OpenPrintTag.

def encode_openprinttag(data_dict):
    """Encode dictionary to CBOR hex string."""
    try:
        # Convert dict to CBOR binary, then to hex string for MQTT transit
        encoded = cbor2.dumps(data_dict)
        return encoded.hex()
    except Exception as e:
        logging.error(f"CBOR Encode Error: {e}")
        return None

ESP32
On the ESP32 side (the YAML I provide), the reader listens for that nfc/readerX/write topic and executes this lambda to perform the physical write:

on_message:
  - topic: nfc/reader1/write
    then:
      - lambda: |-
          // Converts the hex string back to bytes and writes to the tag's NDEF area
          std::vector<uint8_t> data = hex_to_bytes(x);
          id(reader1).write_ndef(data);
