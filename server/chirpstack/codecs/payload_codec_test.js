// Runnable tests for the split ChirpStack payload codecs:
//   payload_codec_rak4631.js  (custom 14-byte frame)
//   payload_codec_rui3.js     (Cayenne LPP, RUI3 channel map)
// Usage: node codecs/payload_codec_test.js   (from server/chirpstack/)
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

function loadCodec(file) {
  const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
  const ctx = { result: {} };
  vm.createContext(ctx);
  vm.runInContext(src + `
    result = {
      decodeUplink: (i) => decodeUplink(i),
      decodeDownlink: (i) => decodeDownlink(i),
      encodeDownlink: (i) => encodeDownlink(i)
    };`, ctx);
  return ctx.result;
}

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; } else { fail++; console.log('FAIL: ' + name + (detail !== undefined ? ' -- ' + JSON.stringify(detail) : '')); }
}
function near(a, b, eps = 1e-6) { return Math.abs(a - b) < eps; }
const ascii = (b) => String.fromCharCode(...b);

// ============================================== RAK4631 (custom frame)

const rak = loadCodec('payload_codec_rak4631.js');

// bat 0x09C4, X=-0.2, Y=0.1, Z=0.01 g, temp 20, status 0x05 (switch + FUNK_ON),
// version 3, threshold 32, duration 2, frequency 47 (10 Hz)
const custom = [0x09,0xC4, 0xFF,0x38, 0x00,0x64, 0x00,0x0A, 0x14, 0x05, 0x03, 0x20, 0x02, 0x2F];
const r1 = rak.decodeUplink({ bytes: custom, fPort: 1 });
const d1 = r1.data;
check('rak: no errors/warnings', r1.errors.length === 0 && r1.warnings.length === 0, r1);
check('rak: battery volts', near(d1.battery_voltage, 0x09C4 * (3.0/4095) * 1.7), d1.battery_voltage);
check('rak: accel', near(d1.accelerometer.x, -0.2) && near(d1.accelerometer.y, 0.1) && near(d1.accelerometer.z, 0.01), d1.accelerometer);
check('rak: temp raw 20', d1.temperature === 20, d1.temperature);
const customNeg = custom.slice(); customNeg[8] = 0xEC; // -20
check('rak: negative temp', rak.decodeUplink({ bytes: customNeg, fPort: 1 }).data.temperature === -20);
check('rak: status bits', d1.motion_switch === 1 && d1.motion_alert === 0 && d1.status_raw === 5, d1);
check('rak: settings fields', d1.version_byte === 3 && d1.acc_threshold === 32 && d1.acc_duration === 2 && d1.acc_frequency === 47, d1);
check('rak: freq 47 -> 10 Hz', d1.acc_frequency_hz === 10, d1.acc_frequency_hz);
const rshort = rak.decodeUplink({ bytes: [1,2,3], fPort: 1 });
check('rak: short frame -> error', rshort.errors.length === 1, rshort);
const rempty = rak.decodeUplink({ bytes: [], fPort: 1 });
check('rak: empty frame -> error', rempty.errors.length === 1, rempty);

check('rak: decodeDownlink 016/p5', rak.decodeDownlink({ bytes: [0x30,0x31,0x36], fPort: 5 }).data.acc_threshold === 16);
const dd6 = rak.decodeDownlink({ bytes: [0x30,0x30,0x32], fPort: 6 });
check('rak: decodeDownlink 002/p6 -> 0 Hz (index not valid)', dd6.data.acc_frequency === 2 && dd6.data.acc_frequency_hz === 0, dd6);
check('rak: decodeDownlink 001/p7', rak.decodeDownlink({ bytes: [0x30,0x30,0x31], fPort: 7 }).data.acc_duration === 1);
check('rak: decodeDownlink 001/p4', rak.decodeDownlink({ bytes: [0x30,0x30,0x31], fPort: 4 }).data.command === 1);
check('rak: decodeDownlink bad ascii -> error', rak.decodeDownlink({ bytes: [0x41,0x42], fPort: 5 }).errors.length === 1);
check('rak: decodeDownlink unknown port -> warning', rak.decodeDownlink({ bytes: [0x30,0x30,0x31], fPort: 9 }).warnings.length === 1);

const e1 = rak.encodeDownlink({ data: { threshold: 16 } });
check('rak: encode threshold 16 -> p5 "016"', e1.fPort === 5 && ascii(e1.outBytes) === '016', e1);
const e2 = rak.encodeDownlink({ data: { duration: 1 } });
check('rak: encode duration 1 -> p7 "001"', e2.fPort === 7 && ascii(e2.outBytes) === '001', e2);
const e3 = rak.encodeDownlink({ data: { frequency: 63 } });
check('rak: encode freq 63 -> p6 "063" (register passthrough)', e3.fPort === 6 && ascii(e3.outBytes) === '063', e3);
const e3b = rak.encodeDownlink({ data: { acc_frequency: 95 } }); // field when the dashboard passes 95 (100 Hz)
check('rak: encode acc_frequency 95 -> p6 "095"', e3b.fPort === 6 && ascii(e3b.outBytes) === '095', e3b);
const e5 = rak.encodeDownlink({ data: { command: 2 } });
check('rak: encode command 2 -> p4 "002"', e5.fPort === 4 && ascii(e5.outBytes) === '002', e5);
check('rak: encode unrecognized -> error', rak.encodeDownlink({ data: { nothing: true } }).errors.length === 1);
const er = rak.encodeDownlink({ data: { acc_threshold: 127 } });
check('rak: round trip threshold 127', rak.decodeDownlink({ bytes: er.outBytes, fPort: er.fPort }).data.acc_threshold === 127);

// ================================================= RUI3 (Cayenne LPP)

const rui = loadCodec('payload_codec_rui3.js');

// ch1 bat 3.12 V, ch2 temp 10.0 C, ch6 accel -0.2/0/0.05 g, ch11 switch 1
const cayFrame = [0x01,0x74,0x01,0x38, 0x02,0x67,0x00,0x64, 0x06,0x71,0xFF,0x38,0x00,0x00,0x00,0x32, 0x0B,0x8E,0x01];
const r2 = rui.decodeUplink({ bytes: cayFrame, fPort: 1 });
const d2 = r2.data;
check('rui: no warnings', r2.warnings.length === 0, r2);
check('rui: battery 3.12 V', near(d2.battery_voltage, 3.12), d2.battery_voltage);
check('rui: temp 10.0', near(d2.temperature, 10.0), d2.temperature);
check('rui: accel', near(d2.accelerometer.x, -0.2) && near(d2.accelerometer.y, 0) && near(d2.accelerometer.z, 0.05), d2.accelerometer);
check('rui: motion_switch 1', d2.motion_switch === 1, d2);

// environment frame: humidity 48.5 %, pressure 800.0 hPa, illuminance 100, gas 44640
const env = [0x03,0x68,0x61, 0x04,0x73,0x1F,0x40, 0x05,0x65,0x00,0x64, 0x0E,0x8A,0xAE,0x60];
const r3 = rui.decodeUplink({ bytes: env, fPort: 1 });
check('rui: humidity 48.5', near(r3.data.humidity, 48.5), r3.data);
check('rui: pressure 800.0', near(r3.data.pressure, 800.0), r3.data);
check('rui: illuminance 100', r3.data.illuminance === 100, r3.data);
check('rui: gas 44640', r3.data.gas_resistance === 44640, r3.data);

// truncated record -> valid prefix + warning, no crash
const rtrunc = rui.decodeUplink({ bytes: [0x01,0x74,0x01,0x38, 0x02,0x67], fPort: 1 });
check('rui: truncated -> prefix decoded + warning', near(rtrunc.data.battery_voltage, 3.12) && rtrunc.warnings.length === 1, rtrunc);

// a RAK4631 custom frame decoded as cayenne must not produce silent garbage
const rc = rui.decodeUplink({ bytes: custom, fPort: 1 });
check('rui: custom frame as bad cayenne -> empty/absurd + warning', rc !== undefined && (rc.warnings.length > 0 || Object.keys(rc.data).length === 0), rc);

check('rui: decodeDownlink 016/p5', rui.decodeDownlink({ bytes: [0x30,0x31,0x36], fPort: 5 }).data.acc_threshold === 16);
const dd6r = rui.decodeDownlink({ bytes: [0x30,0x30,0x32], fPort: 6 });
check('rui: decodeDownlink 002/p6 -> 10 Hz', dd6r.data.acc_frequency === 2 && dd6r.data.acc_frequency_hz === 10, dd6r);
check('rui: decodeDownlink port 4 unknown -> warning', rui.decodeDownlink({ bytes: [0x30,0x30,0x31], fPort: 4 }).warnings.length === 1);

const f1 = rui.encodeDownlink({ data: { threshold: 16 } });
check('rui: encode threshold 16 -> p5 "016"', f1.fPort === 5 && ascii(f1.outBytes) === '016', f1);
const f2 = rui.encodeDownlink({ data: { frequency: 63 } });
check('rui: encode register 63 -> p6 "003" (ODR index)', f2.fPort === 6 && ascii(f2.outBytes) === '003', f2);
const f3 = rui.encodeDownlink({ data: { frequency: 2 } });
check('rui: encode index 2 passthrough -> p6 "002"', f3.fPort === 6 && ascii(f3.outBytes) === '002', f3);
const f4 = rui.encodeDownlink({ data: { frequency: 99, } });
check('rui: unknown freq -> warning', f4.warnings.length === 1, f4);
const f5 = rui.encodeDownlink({ data: { duration: 1 } });
check('rui: encode duration 1 -> p7 "001"', f5.fPort === 7 && ascii(f5.outBytes) === '001', f5);
check('rui: encode unrecognized -> error', rui.encodeDownlink({ data: { nothing: true } }).errors.length === 1);

// ============================== real captured uplinks (../fixtures/)
// ChirpStack MQTT up events: "object" was produced by these very codecs;
// decode the raw payload and require at least that output (superset: the
// rui3 codec now also adds acc_frequency_hz on uplinks).
const LOGS = [
  ['../fixtures/single-log(rak4631).json', rak, { motion_switch: 0, motion_alert: 0, battery_voltage: 3.578095238095238, temperature: 32, acc_frequency_hz: 10 }],
  ['../fixtures/single-log(rak4631 alarm).json', rak, { motion_switch: 0, motion_alert: 1, status_raw: 2, acc_frequency: 47 }],
  ['../fixtures/single-log(RUI).json', rui, { battery_voltage: 2.98, temperature: 23.1, humidity: 43, acc_threshold: 16, acc_duration: 4, acc_frequency_hz: 100 }],
  ['../fixtures/single-log(RUI alarm).json', rui, { motion_alert: 1 }],
];
for (const [file, codec, extra] of LOGS) {
  const ev = JSON.parse(fs.readFileSync(path.join(__dirname, file), 'utf8'));
  const dec = codec.decodeUplink({ bytes: Uint8Array.from(Buffer.from(ev.data, 'base64')), fPort: ev.fPort });
  check('log ' + file + ': no warnings/errors', dec.warnings.length === 0 && dec.errors.length === 0, dec);
  for (const [k, v] of Object.entries(ev.object)) {
    check('log ' + file + ': ' + k, typeof v === 'object'
      ? JSON.stringify(dec.data[k]) === JSON.stringify(v) ||
        near(dec.data[k].x, v.x) && near(dec.data[k].y, v.y) && near(dec.data[k].z, v.z)
      : (typeof v === 'number' ? near(dec.data[k], v, 1e-9) : dec.data[k] === v),
      { mine: dec.data[k], logged: v });
  }
  for (const [k, v] of Object.entries(extra)) {
    check('log ' + file + ': extra ' + k, typeof v === 'number' ? near(dec.data[k], v) : dec.data[k] === v, { mine: dec.data[k], expected: v });
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
