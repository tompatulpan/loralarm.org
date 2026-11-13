/**
 * ChirpStack device-profile payload codec (Custom JavaScript codec)
 * for the **Browan TBWL100** water-leak sensor (Tabs), reference manual
 * BQW_02_0012.002. Uplink only; the sensor is configured with downlinks
 * on fPort 204, which this codec does not encode.
 *
 * Status uplink, fPort 106, 5 bytes:
 *   0  status flags: bit0 = water leak (1) / dry (0), bit4 = leak
 *      interrupt, bit5 = temperature changed (2 C delta), bit6 = RH
 *      changed (5 %RH delta); bits 1-3 and 7 are RFU
 *   1  battery, bits 3:0 = v (1-14): volts = (25 + v) / 10; bits 7:4 RFU
 *   2  PCB temperature, bits 6:0 - 32 = C (NTC on the board)
 *   3  relative humidity, bits 6:0 in %; 127 = measurement error
 *   4  environment temperature, bits 6:0 - 32 = C
 *
 * Config response, fPort 204, 10 bytes (answer to the "get sensor
 * configuration" downlink): keep-alive s, temperature delta, RH delta,
 * detection interval s, each preceded by its command byte.
 */

function decodeUplink(input) {
  const b = input.bytes || [];
  if (b.length === 0) {
    return { data: {}, warnings: [], errors: ['empty payload'] };
  }

  switch (input.fPort) {
    case 106:
      return decodeStatus(b);
    case 204:
      return decodeConfig(b);
    default:
      return { data: {}, warnings: [], errors: ['TBWL100: unknown fPort ' + input.fPort] };
  }
}

function decodeStatus(b) {
  if (b.length < 5) {
    return { data: {}, warnings: [], errors: ['TBWL100 status frame too short: ' + b.length + ' bytes, need 5'] };
  }
  const warnings = [];
  const rh = b[3] & 0x7f;
  if (rh === 127) {
    warnings.push('humidity measurement error');
  }
  return {
    data: {
      status: b[0] & 0x01,
      leakInterrupt: (b[0] >> 4) & 0x01,
      temperatureChanged: (b[0] >> 5) & 0x01,
      humidityChanged: (b[0] >> 6) & 0x01,
      battery: (25 + (b[1] & 0x0f)) / 10,
      temperatureBoard: (b[2] & 0x7f) - 32,
      humidity: rh === 127 ? null : rh,
      temperature: (b[4] & 0x7f) - 32
    },
    warnings: warnings,
    errors: []
  };
}

function decodeConfig(b) {
  if (b.length < 10) {
    return { data: {}, warnings: [], errors: ['TBWL100 config frame too short: ' + b.length + ' bytes, need 10'] };
  }
  return {
    data: {
      keepAliveInterval: b[1] | b[2] << 8, // s, little-endian
      temperatureDelta: b[4],
      humidityDelta: b[6],
      detectionInterval: b[8] | b[9] << 8 // s, little-endian
    },
    warnings: [],
    errors: []
  };
}
