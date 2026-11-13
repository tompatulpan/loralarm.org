# server/

Kod för LoRaLarm-serverdelen: basenheter/gateways, nätverksserver och integrationslager.

Se även kunskapsbasen kring ChirpStack/Mosquitto-setup på "lora"-boxen för referensarkitekturen.

| Directory | Contents |
|---|---|
| `chirpstack/` | ChirpStack payload codecs (`codecs/`), captured uplink fixtures (`fixtures/`), the Node-RED → ChirpStack/InfluxDB/Python migration spec |
| `deploy/` | Deployment artifacts: `loralarm.service`, env template, install/deploy scripts; `README.md` (Debian LXC flow) and `loralarm-service-deployment-guide.md` (dedicated host via git checkout) |
| `node-red/` | Legacy Node-RED flows (migration source) |
| `python/` | The loralarm Python service (ChirpStack MQTT → notifications, SQLite event store, web dashboard); see `python/README.md` and `python/ARCHITECTURE.md` |
