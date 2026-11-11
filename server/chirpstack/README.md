# server/chirpstack/

Payload codecs for the ChirpStack **device profiles**, one per sensor
family -- the device profile already pins the protocol, so each codec
is unambiguous. Ported 1:1 from the Node-RED unified flow's
`decode uplink` / Cayenne LPP logic and the buffer-parser chain for the
custom RAK4631 frame.

## Files

| File | Sensor | Uplink |
|---|---|---|
| `payload_codec_rak4631.js` | RAK4631 (Arduino/Android) | custom 14-byte binary frame |
| `payload_codec_rui3.js` | RUI3 | Cayenne LPP (`devices/rui3_sensor/app.h` channel map) |
| `payload_codec_dragino_lds02.js` | Dragino LDS02 door switch | third-party format, fPort 10 (`DOOR_OPEN_STATUS`, `BAT_V`) |
| `payload_codec_test.js` | runnable test: `node payload_codec_test.js` | |

`fixtures/` holds four captured real ChirpStack MQTT up events — one
heartbeat and one alarm uplink each for a RAK4631 (`sensor3`) and a
RUI3 sensor (`sensor16`). The test decodes their raw payloads and
asserts the codec output matches the logged `object` field, so real
firmware frames serve as regression fixtures. They are tracked in git
(the Python service tests replay them too:
`server/python/tests/test_golden.py`); the old git-ignored `log/` name is gone.

The Dragino LDS02 codec (`payload_codec_dragino_lds02.js`) is a third-party
decoder, fPort 10; its decoded objects are the inline fixtures in
`server/python/tests/test_lds02.py` and the golden scenarios.

## Install in ChirpStack

1. Device profile of the matching family → **Payload codec** →
   **Custom javascript codec**.
2. Paste the contents of the family's codec file.
3. Paste the other family's codec into its own device profile.

No configuration constants (the former `PROTOCOL` / `auto` of the
combined codec were removed -- the profile selects the family). One
consequence: in `payload_codec_rui3.js` the frequency downlink map
(RAK4631 register codes 31/47/63/79/95 → RUI3 ODR index 1–5) is applied
deterministically, instead of the Node-RED `auto` mode having to guess
after the first uplink.

## What the codecs output

A uniform object in both files, the same field names the Node-RED
Cayenne decoder produces:

```
battery_voltage          V (RAK4631: raw ADC * 3.0/4095 * 1.7)
accelerometer {x,y,z}    g
temperature              °C, UNCERTAIN — Tk offset NOT applied (per-sensor env in Node-RED)
motion_switch            0/1  (RAK4631: status bit 0)
motion_alert             0/1  (RAK4631: status bit 1)
version_byte, acc_threshold, acc_duration, acc_frequency
acc_frequency_hz         normalized ODR (RAK4631 register 31/47/… and RUI3 index 1–7 both mapped)
humidity, pressure, illuminance, gas_resistance   (RUI3 environment channels)
```

Downlink direction (3-digit ASCII decimal):

| port | parameter | RAK4631 | RUI3 |
|---|---|---|---|
| 4 | command (high/low, test alarm) | yes | – |
| 5 | interrupt threshold | 1–127, `"016"` | 1–127, `"016"` |
| 6 | ODR frequency | register 31/47/63/79/95 | index 1–7, `"002"` = 10 Hz |
| 7 | interrupt duration | 0–127, `"001"` | 0–127, `"001"` |

`encodeDownlink` accepts `data.threshold|duration|frequency|command`
(or `acc_*` names) and produces those 3-byte ASCII payloads; both
`outBytes` (ChirpStack ≥ 4.6) and `bytes` (older) are returned.

## Relation to Node-RED — important

The codecs are **additive**: ChirpStack MQTT uplink events keep the raw
base64 payload in `data` and add the decoded object in `object`, so the
existing sensor flows keep working unchanged. The per-sensor bits that
ChirpStack cannot know stay in Node-RED:

* `Tk` temperature offset (codec emits the raw reading)
* `battFull`/`battWarn`/`battEmpty` battery scaling (codec emits volts)
* alarm counting, notification targets, dashboard

(The Node-RED unified subflow keeps its internal `auto` protocol
handling -- it serves both families on one flow instance and does not
know which device profile an uplink came from.)

The Node-RED flows could later be simplified to consume `object`
directly instead of the buffer-parser chain. `payload_codec_test.js`
keeps the frame parsing testable without a live ChirpStack:

```bash
node payload_codec_test.js   # 43 checks
```

Note: the codec sandbox is plain JavaScript — no `Buffer`, no
`env.get`, no persistent state between calls, no `console`; errors are
reported through the `errors` array.
