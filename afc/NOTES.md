# Ideas for Writing Updates on Filament Usage to an OpenPrintTag

## The Write Logic to OpenPrintTag
Function to be triggered **60 seconds** after a filament unload is detected. This delay ensures that the final usage data is correctly recorded in Spoolman before being written back to the physical tag.
<<<<<<< HEAD
=======

> **Alternative to fixed delay:** Instead of a hard 60-second timer, consider
> polling Spoolman a couple of times and writing once `remaining_length` has
> stabilized (hasn't changed for ~10 seconds). This handles cases where a long
> retract or purge takes longer than expected.
>>>>>>> 623b530 (Update from Mac)

```python
def write_usage_to_tag(lane_id, spool_id):
    """Fetch current usage from Spoolman and write remaining length to NFC tag."""
    try:
        # 1. Get the latest remaining_length from Spoolman (already in meters)
        #    Spoolman computes this internally from filament density/diameter,
        #    so the middleware never needs to do that math itself.
        resp = requests.get(f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}")
        if resp.status_code == 200:
            spool = resp.json()
            remaining_length = spool.get("remaining_length")

            if remaining_length is None:
                logging.warning(f"No remaining_length for spool {spool_id}")
                return

            # 2. Determine which reader handles this lane
            reader_id = "reader1" if lane_id in [1, 2] else "reader2"

            # 3. Construct the OpenPrintTag payload
            #    Use the correct CBOR integer key for remaining_length
            #    per the OpenPrintTag spec (see specs.openprinttag.org)
            payload = {KEY_REMAINING_LENGTH: round(remaining_length, 2)}
            hex_data = encode_openprinttag(payload)

            if hex_data:
                # 4. Publish targeted write to the ESPHome write topic
                #    Include target_uid so the ESP32 validates the tag
                #    before writing (see Targeted Write section below)
                target_uid = get_lane_uid(lane_id)
                write_msg = json.dumps({
                    "target_uid": target_uid,
                    "ndef_data": hex_data
                })
                topic = f"nfc/{reader_id}/write"
                client.publish(topic, write_msg)
                logging.info(f"Wrote {remaining_length:.2f}m to tag on Lane {lane_id} via {reader_id}")
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
          // Parse JSON: {"target_uid": "...", "ndef_data": "..."}
          // Run anti-collision, select tag by target_uid
          // If match: convert hex ndef_data to bytes, write to NDEF area
          // If no match: publish failure to nfc/reader1/write_result
          auto json = parse_json(x);
          std::string target = json["target_uid"];
          std::string data = json["ndef_data"];
          auto tags = id(reader1).get_tags_in_field();
          if (find_tag_by_uid(tags, target)) {
            auto bytes = hex_to_bytes(data);
            id(reader1).write_ndef(bytes);
            // publish success
          } else {
            // publish failure — target tag not in field
          }
```

---

## Proposed Module: `openprinttag.py`

The OpenPrintTag encode/decode logic, CBOR key mapping, and data validation should live in a dedicated module, separate from the main middleware. This keeps `nfc_listener.py` focused on MQTT/Spoolman

Responsibilities:
- **CBOR encode/decode** — wraps `cbor2` with OpenPrintTag's integer key mapping
- **Key constants** — maps OpenPrintTag spec keys (remaining_length, material, color, etc.) to their integer IDs per `specs.openprinttag.org`
- **Read tag data** — decode an OpenPrintTag NDEF payload into a Python dict with human-readable keys
- **Build write payloads** — construct valid CBOR payloads for writing back to tags (e.g. updated remaining_length)
- **Reconciliation helpers** — compare tag data vs Spoolman data and return a recommended action (see Reconciliation section below

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

The PN5180 supports anti-collision with UID-specific selection in a multi-tag field. The flow is:
1. Receive write command with target UID over MQTT
2. Run anti-collision to enumerate all tags in the field
3. Select the matching UID specifically
4. Write the NDEF data
5. If target UID is not present, abort and publish a failure message back over MQTT

*   **Match Found:** It performs the write.
*   **No Match:** (e.g., the tag was swapped during the 60-second window) It aborts the write and logs a warning to prevent data corruption.

<<<<<<< HEAD
This is something I actually have already been pondering for loading filament and having mismatches.

If Tag Usage > Spoolman Usage: The spool was likely used elsewhere. We update Spoolman to match the tag so you don't run out of filament mid-print.

If Tag Usage < Spoolman Usage: Same issue as above. Log a warning but stick with Spoolman's higher number
=======
#### Retry Queue
If the tag isn't in the field at write time (spool was already removed), the middleware should queue the pending write. Next time that UID shows up on any reader, the queued write executes. This covers the common case where someone pulls a spool before the 60-second timer fires.

---

## Usage Reconciliation: Tag vs Spoolman

When reading an OpenPrintTag, compare the tag's data against Spoolman before trusting either source blindly.

### Scenario 1: Tag has less remaining than Spoolman
The spool was likely used elsewhere (different printer, not tracked). **Update Spoolman to match the tag** — this is the safer number and prevents running out of filament mid-print.

### Scenario 2: Tag has more remaining than Spoolman
Spoolman tracked more usage than the tag knows about. Log a warning but **stick with Spoolman's lower number** — it's the more conservative estimate.

### Scenario 3: Tag data doesn't match the spool at all
The tag could be on a completely different spool than what Spoolman thinks — someone re-tagged a spool, stuck a tag on the wrong one, or the tag was rewritten elsewhere. **Compare material type and color** from the OpenPrintTag data against what Spoolman has for that spool ID. If they don't match, this is a hard mismatch:
- **Block the write** — do not update either the tag or Spoolman
- **Alert the user** — log an error and optionally flash the lane LED red
- **Require manual resolution** — the user needs to either re-tag the spool or correct Spoolman
>>>>>>> 623b530 (Update from Mac)

