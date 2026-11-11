/**
 * ChirpStack device-profile payload codec (Custom JavaScript codec)
 * for the **Dragino LDS02** door / water-leak sensor -- third-party
 * codec by Dragino, uplink on fPort 10. Unlike the RAK4631 and RUI3
 * families (payload_codec_rak4631.js / payload_codec_rui3.js, both
 * written for this project) this decoder is the vendor-supplied one,
 * kept verbatim in its field names because the Python service maps
 * them in `families/dragino_lds02.json` (`field_map`:
 * BAT_V -> battery_voltage, DOOR_OPEN_STATUS -> door_open).
 *
 * ChirpStack v4 codec interface:
 *   decodeUplink(input)  -> {data, warnings, errors}
 *   decodeDownlink(input)-> {data, warnings, errors}
 *   encodeDownlink(input)-> {outBytes, fPort, warnings, errors}
 *
 * The codec stays **stateless**: per-device things (battery limits
 * 3.0/2.5/2.2 V for 2xAAA, notification targets, door vs leak kind)
 * remain in the Python service -- ChirpStack gives the codec no
 * per-device storage or env variables.
 *
 * No downlink functions are provided: the LDS02 family definition has
 * `"settings": []` -- heartbeat interval etc. are configured on the
 * device itself, not pushed from the server.
 */

// ------------------------------------------------------------ uplink
//
// 10-byte frame on fPort 10:
//   0-1  uint16be battery, millivolts; top 2 bits are flags:
//        bit7 (0x80) = door open, bit6 (0x40) = water leak
//        (battery = (b[0]<<8 | b[1]) & 0x3FFF, in mV -> V /1000)
//   2    MOD: 1 = door event, 2 = leak event, 3 = heartbeat/status
//   3-5  event count      (24-bit, meaning depends on MOD)
//   6-8  last duration    (24-bit, minutes, meaning depends on MOD)
//   9    ALARM bit (bit0)  (only meaningful in MOD 1 and 3)
//
// MOD 1 (door):   3-5 = DOOR_OPEN_TIMES, 6-8 = LAST_DOOR_OPEN_DURATION (min)
// MOD 2 (leak):   3-5 = WATER_LEAK_TIMES, 6-8 = LAST_WATER_LEAK_DURATION (min)
// MOD 3 (status): heartbeat; same 10-byte layout also carries ALARM
//
// The leak bit and MOD 2 belong to the **LWL02** water-leak sensor, which
// runs the same firmware (and therefore sends the same frame) as the
// LDS02 -- a door-only LDS02 only ever sends MOD 1 and MOD 3. In this
// deployment the LDS02 profile is assigned to door sensors only
// (Door1/Door2); water1/water2 are a different device family.

function decodeUplink(input) {
  const b = input.bytes || [];
  if (b.length === 0) {
    return { data: {}, warnings: [], errors: ['empty payload'] };
  }
  if (b.length < 10) {
    return { data: {}, warnings: [], errors: ['LDS02 frame too short: ' + b.length + ' bytes, need 10'] };
  }
  const result = Decode(input.fPort, b, input.variables);
  if (result === undefined) {
    return { data: {}, warnings: [], errors: ['LDS02: unrecognized MOD ' + b[2] + ' or frame length ' + b.length] };
  }
  return { data: result, warnings: [], errors: [] };
}

function Decode(fPort, bytes, variables) {
    // Decode an uplink message from a buffer
    // (array) of bytes to an object of fields.
    var value=(bytes[0]<<8 | bytes[1])&0x3FFF;
    var bat=value/1000;//Battery,units:V

    var door_open_status=bytes[0]&0x80?1:0;//1:open,0:close
    var water_leak_status=bytes[0]&0x40?1:0;

    var mod=bytes[2];
    var alarm=bytes[9]&0x01;

    if(mod==1){
        var open_times=bytes[3]<<16 | bytes[4]<<8 | bytes[5];
        var open_duration=bytes[6]<<16 | bytes[7]<<8 | bytes[8];//units:min
        if(bytes.length==10)
            return {
                Node_type:"LDS02",
                BAT_V:bat,
                MOD:mod,
                DOOR_OPEN_STATUS:door_open_status,
                DOOR_OPEN_TIMES:open_times,
                LAST_DOOR_OPEN_DURATION:open_duration,
                ALARM:alarm
            };
    }
    else if(mod==2)
    {
        var leak_times=bytes[3]<<16 | bytes[4]<<8 | bytes[5];
        var leak_duration=bytes[6]<<16 | bytes[7]<<8 | bytes[8];//units:min
        if(bytes.length==10)
            return {
                Node_type:"LDS02",
                BAT_V:bat,
                MOD:mod,
                WATER_LEAK_STATUS:water_leak_status,
                WATER_LEAK_TIMES:leak_times,
                LAST_WATER_LEAK_DURATION:leak_duration
            };
    }
    else if(mod==3)
        if(bytes.length==10)
        {
            return {
                Node_type:"LDS02",
                BAT_V:bat,
                MOD:mod,
                DOOR_OPEN_STATUS:door_open_status,
                WATER_LEAK_STATUS:water_leak_status,
                ALARM:alarm
            };
        }
        else{
            return {
                Node_type:"LDS02",
                BAT_V:bat,
                MOD:mod,
            };
        }
}
