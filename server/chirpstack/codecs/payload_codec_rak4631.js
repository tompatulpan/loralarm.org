/**
 * ChirpStack device-profile payload codec (Custom JavaScript codec)
 * for the RAK4631 (Arduino/Android) sensors -- custom 14-byte binary
 * frame, uplink port 1. Cayenne is NOT handled here: the RUI3 sensors
 * use payload_codec_rui3.js.
 *
 * ChirpStack v4 codec interface:
 *   decodeUplink(input)  -> {data, warnings, errors}
 *   decodeDownlink(input)-> {data, warnings, errors}
 *   encodeDownlink(input)-> {outBytes, fPort, warnings, errors}
 * ('bytes' is also returned for ChirpStack < 4.6 compat.)
 *
 * The codec stays **stateless**: per-sensor things (Tk temperature
 * offset, battery volts->%, notification targets) remain in Node-RED
 * -- ChirpStack gives the codec no per-device storage or env variables.
 */

const u16 = (b, i) => (b[i] << 8) | b[i + 1];
const s16 = (b, i) => { const v = u16(b, i); return v >= 0x8000 ? v - 0x10000 : v; };

// LIS3DH ODR register value (what this firmware and the dashboard use)
// -> Hz
function freqHz(registerValue) {
  const REG = { 31: 1, 47: 10, 63: 25, 79: 50, 95: 100 };
  return REG[registerValue] !== undefined ? REG[registerValue] : 0;
}

// ------------------------------------------------------------ uplink
//
// 14-byte custom frame:
//   0-1  uint16be battery ADC  (volts = raw * (3.0 / 4095) * 1.7)
//   2-3  int16be X (0.001 g)      4-5: Y        6-7: Z
//   8    int8 temperature, raw (Tk offset is applied in Node-RED)
//   9    status byte: bit0 = switch alarm, bit1 = accelerometer alarm,
//        bit2 = FUNK_ON, bits3-7 reserved
//   10   version
//   11   interrupt threshold
//   12   interrupt duration
//   13   accelerometer frequency (LIS3DH ODR register value)

function decodeUplink(input) {
  const b = input.bytes || [];
  if (b.length === 0) {
    return { data: {}, warnings: [], errors: ['empty payload'] };
  }
  if (b.length < 14) {
    return { data: {}, warnings: [], errors: ['custom frame too short: ' + b.length + ' bytes, need 14'] };
  }

  const status = b[9];
  const frequency = b[13];
  return {
    data: {
      battery_voltage: u16(b, 0) * (3.0 / 4095) * 1.7,
      accelerometer: { x: s16(b, 2) / 1000, y: s16(b, 4) / 1000, z: s16(b, 6) / 1000 },
      temperature: b[8] >= 0x80 ? b[8] - 0x100 : b[8],   // raw, no Tk
      motion_switch: (status & 0x01) !== 0 ? 1 : 0,
      motion_alert: (status & 0x02) !== 0 ? 1 : 0,
      status_raw: status,
      version_byte: b[10],
      acc_threshold: b[11],
      acc_duration: b[12],
      acc_frequency: frequency,
      acc_frequency_hz: freqHz(frequency)
    },
    warnings: [],
    errors: []
  };
}

// ---------------------------------------------------------------- downlink
//
// 3-byte ASCII decimal on fixed ports:
//   port 4: command (high/low, test alarm)
//   port 5: interrupt threshold  1-127   ("016")
//   port 6: ODR frequency, LIS3DH register value 31/47/63/79/95
//   port 7: interrupt duration   0-127   ("001")

function decodeDownlink(input) {
  const b = input.bytes || [];
  const text = String.fromCharCode.apply(null, b).trim();
  const value = parseInt(text, 10);
  const ports = {
    4: 'command',
    5: 'acc_threshold',
    6: 'acc_frequency',
    7: 'acc_duration'
  };
  const name = ports[input.fPort];
  if (name === undefined) {
    return { data: {}, warnings: ['unknown downlink port ' + input.fPort], errors: [] };
  }
  if (isNaN(value)) {
    return { data: {}, warnings: [], errors: ['downlink is not ASCII decimal: "' + text + '"'] };
  }
  const data = {};
  data[name] = value;
  if (name === 'acc_frequency') data.acc_frequency_hz = freqHz(value);
  return { data: data, warnings: [], errors: [] };
}

// dashboard frequency codes are the LIS3DH register values this firmware
// expects -- pass them through verbatim
function encodeDownlink(input) {
  const data = input.data || {};
  let fPort = input.fPort;
  let payload = null;

  if (data.acc_threshold !== undefined || data.threshold !== undefined) {
    fPort = 5;
    payload = data.acc_threshold !== undefined ? data.acc_threshold : data.threshold;
  } else if (data.acc_frequency !== undefined || data.frequency !== undefined) {
    fPort = 6;
    payload = data.acc_frequency !== undefined ? data.acc_frequency : data.frequency;
  } else if (data.acc_duration !== undefined || data.duration !== undefined) {
    fPort = 7;
    payload = data.acc_duration !== undefined ? data.acc_duration : data.duration;
  } else if (data.command !== undefined) {
    fPort = 4;
    payload = data.command;
  }

  if (payload === null) {
    return { outBytes: [], bytes: [], fPort: fPort, warnings: [],
             errors: ['unrecognized downlink object: ' + JSON.stringify(data)] };
  }

  const text = String(parseInt(payload, 10) || 0).padStart(3, '0');
  const outBytes = [];
  for (let i = 0; i < text.length; i++) outBytes.push(text.charCodeAt(i));

  return {
    outBytes: outBytes,
    bytes: outBytes,     // < ChirpStack 4.6 compat
    fPort: fPort,
    warnings: [],
    errors: []
  };
}
