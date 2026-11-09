/**
 * ChirpStack device-profile payload codec (Custom JavaScript codec)
 * for the RUI3 sensors -- Cayenne LPP uplinks with the channel map of
 * devices/rui3_sensor/app.h. The RAK4631 custom frame is NOT handled
 * here: those sensors use payload_codec_rak4631.js.
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

// ------------------------------------------------------------ Cayenne LPP

const CHANNELS = {
  1: 'battery_voltage', 2: 'temperature', 3: 'humidity', 4: 'pressure',
  5: 'illuminance', 6: 'accelerometer', 7: 'version_byte', 8: 'acc_threshold',
  9: 'acc_duration', 10: 'acc_frequency', 11: 'motion_switch',
  12: 'motion_alert', 13: 'low_battery_shutdown', 14: 'gas_resistance'
};
const TYPE_NAMES = {
  0x00: 'digital_input', 0x01: 'digital_output', 0x8E: 'switch_output',
  0x65: 'illuminance', 0x67: 'temperature', 0x68: 'humidity',
  0x71: 'accelerometer', 0x73: 'barometric_pressure', 0x74: 'battery_voltage',
  0x86: 'gyrometer', 0x88: 'gps', 0x8A: 'gas_resistance'
};
// Cayenne LPP type -> data size in bytes
const SIZES = {
  0x00: 1, 0x01: 1, 0x8E: 1, 0x65: 2, 0x67: 2, 0x68: 1,
  0x71: 6, 0x73: 2, 0x74: 2, 0x86: 6, 0x88: 9, 0x8A: 2
};

const u16 = (b, i) => (b[i] << 8) | b[i + 1];
const s16 = (b, i) => { const v = u16(b, i); return v >= 0x8000 ? v - 0x10000 : v; };

function lppValue(type, b, i) {
  switch (type) {
    case 0x00: case 0x01: case 0x8E: return b[i];
    case 0x65: return u16(b, i);
    case 0x67: return s16(b, i) / 10;           // 0.1 C
    case 0x68: return b[i] / 2;                 // 0.5 %
    case 0x73: return u16(b, i) / 10;           // 0.1 hPa
    case 0x74: return u16(b, i) / 100;          // 0.01 V
    case 0x71: return { x: s16(b, i) / 1000, y: s16(b, i + 2) / 1000, z: s16(b, i + 4) / 1000 };
    case 0x86: return { x: s16(b, i) / 100, y: s16(b, i + 2) / 100, z: s16(b, i + 4) / 100 };
    case 0x8A: return u16(b, i);
  }
}

// Decode as far as possible; a warning is reported for the first unknown
// or truncated record and the valid prefix is returned.
function decodeUplink(input) {
  const b = input.bytes || [];
  const out = {};
  const warnings = [];
  let i = 0;
  while (i < b.length) {
    const ch = b[i];
    const type = b[i + 1];
    const size = SIZES[type];
    if (i + 1 >= b.length || size === undefined || i + 2 + size > b.length) {
      warnings.push('Cayenne: unknown or truncated record (type 0x' +
        Number(type).toString(16) + ', channel ' + ch + ') at byte ' + i);
      break;
    }
    out[CHANNELS[ch] || (TYPE_NAMES[type] + '_' + ch)] = lppValue(type, b, i + 2);
    i += 2 + size;
  }
  return { data: out, warnings: warnings, errors: [] };
}

// RUI3 ODR index 1-7 (what this firmware expects) -> Hz
function freqHz(index) {
  const IDX = { 1: 1, 2: 10, 3: 25, 4: 50, 5: 100, 6: 200, 7: 400 };
  return IDX[index] !== undefined ? IDX[index] : 0;
}

// ---------------------------------------------------------------- downlink
//
// 3-byte ASCII decimal on fixed ports (devices/rui3_sensor/README.md):
//   port 5: interrupt threshold  1-127   ("016")
//   port 6: ODR frequency index  1-7     ("002" = 10 Hz)
//   port 7: interrupt duration   0-127   ("001")

function decodeDownlink(input) {
  const b = input.bytes || [];
  const text = String.fromCharCode.apply(null, b).trim();
  const value = parseInt(text, 10);
  const ports = {
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

// dashboard frequency codes are the RAK4631 register values (31/47/63/
// 79/95); this firmware expects the ODR index 1-7 on the same port -- the
// mapping is safe here because the device profile (unlike the Node-RED
// "auto" flow) already knows the sensor is RUI3
const REG_TO_RUI3 = { 0: 0, 31: 1, 47: 2, 63: 3, 79: 4, 95: 5 };

function encodeDownlink(input) {
  const data = input.data || {};
  const warnings = [];
  let fPort = input.fPort;
  let payload = null;

  if (data.acc_threshold !== undefined || data.threshold !== undefined) {
    fPort = 5;
    payload = data.acc_threshold !== undefined ? data.acc_threshold : data.threshold;
  } else if (data.acc_frequency !== undefined || data.frequency !== undefined) {
    fPort = 6;
    let v = data.acc_frequency !== undefined ? data.acc_frequency : data.frequency;
    if (REG_TO_RUI3[v] !== undefined) v = REG_TO_RUI3[v];
    else if (!(v >= 1 && v <= 7)) warnings.push('unknown frequency code: ' + v);
    payload = v;
  } else if (data.acc_duration !== undefined || data.duration !== undefined) {
    fPort = 7;
    payload = data.acc_duration !== undefined ? data.acc_duration : data.duration;
  }

  if (payload === null) {
    return { outBytes: [], bytes: [], fPort: fPort, warnings: warnings,
             errors: ['unrecognized downlink object: ' + JSON.stringify(data)] };
  }

  const text = String(parseInt(payload, 10) || 0).padStart(3, '0');
  const outBytes = [];
  for (let i = 0; i < text.length; i++) outBytes.push(text.charCodeAt(i));

  return {
    outBytes: outBytes,
    bytes: outBytes,     // < ChirpStack 4.6 compat
    fPort: fPort,
    warnings: warnings,
    errors: []
  };
}
