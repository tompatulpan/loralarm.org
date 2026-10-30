---
title: "Installation"
---

> **[Dokumentation under arbete]** – uppdaterad 2026-10-30. Vi följer upp den gamla beskrivningen (Ubuntu 20.04, ChirpStack v3-tid) mot dagens läge.

## Mjukvarustacken för backend/server

- [ChirpStack](https://www.chirpstack.io/), "LoRaWAN Network Server"
- [Mosquitto](https://mosquitto.org/), MQTT-broker som ChirpStack och Node-RED kommunicerar via
- [Node-RED](https://nodered.org/), för att analysera och visa data via ett webbinterface
- [ntfy](https://ntfy.sh/), för att ta emot pushnotiser på Android och dator
- [Pushover](https://pushover.net/), för att ta emot pushnotiser på framför allt Apples produkter (ntfy på iOS kräver uppströmsserver mot ntfy.sh och kan vara mindre pålitlig). Notera att Pushover är proprietär programvara!

## Alternativ att överväga idag (2026)

- **Docker Compose i stället för paketinstallation:** ChirpStack erbjuder en färdig [docker-compose-miljö](https://www.chirpstack.io/docs/getstarted/install.html) med ChirpStack, Mosquitto, PostgreSQL och Redis i ett uppsätt. Enklast att komma igång med, och uppgraderingar sker med `docker compose pull` i stället för manuella steg.
- **Node-RED kan vara valfritt:** ChirpStack v4 har inbyggda integrationer (MQTT, HTTP, med mera), så enklare vidarebefordran till t.ex. ntfy kan skrivas direkt mot MQTT eller HTTP utan Node-RED. Node-RED är fortfarande smidigt när man vill bygga flöden och dashboards utan kod.
- **The Things Stack (TTN) som nätverksserver:** Om man inte vill drifta egen server kan [The Things Network](https://www.thethingsnetwork.org/) användas gratis; se menyn "Fler möjligheter" på startsidan.

## Serverinstallation

- Installation och konfiguration sker på en och samma maskin – i denna beskrivning **Ubuntu Server 24.04 LTS**. (Den tidigare beskrivningen använde 20.04, som nådde slutet av standardstödet i maj 2025.)
- Testat i VM, men ska inte vara något problem på ex. en Raspberry Pi.
- Sätt upp en användare utöver root för alla installationer.

```
# logga in som root

# Uppdatera systemet
# apt update && apt upgrade -y

# Lägg till användare som "super user"
# adduser <användarnamn>
# usermod -aG sudo <användarnamn>

# reboot
```

## ChirpStack

### Installation

- Vi följer [denna](https://www.chirpstack.io/docs/getting-started/debian-ubuntu.html) instruktion (ChirpStack v4). Glöm inte Mosquitto som broker – den ingår i guiden.

### Konfiguration

I ChirpStack v4 ligger integrationerna under `[integrations]` (plural), till skillnad från v3 som använde `[integration]`:

```
$ sudo nano /etc/chirpstack/chirpstack.toml

[integrations]
  enabled = ["mqtt"]

  [integrations.mqtt]
    server = "tcp://localhost:1883/"
    json = true
```

```
# starta chirpstack automatiskt vid upstart
$ sudo systemctl enable chirpstack

# starta chirpstack
$ sudo systemctl start chirpstack
```

## Node-RED

### Installation

- Idag rekommenderas [installationsskriptet](https://nodered.org/docs/getting-started/local) framför manuell npm-installation. Skriptet installerar även en passerande Node.js-version och sätter upp Node-RED som systemd-tjänst:

```
$ bash <(curl -sL https://raw.githubusercontent.com/node-red/linux-installers/master/deb/update-nodejs-and-nodered)
```

- (Den äldre vägen, `sudo apt install nodejs && sudo npm install -g --unsafe-perm node-red`, ger ofta en för gammal Node.js – Ubuntu 24.04 levererar Node 18 och Node-RED 4.x vill ha Node 18.5+ eller 20/22 – och Node-RED startas då i förgrunden i stället som tjänst.)

### Konfiguration

```
# Sätt upp en admin om sidan ska visas publikt!
$ sudo nano ~/.node-red/settings.js

adminAuth: {
    type: "credentials",
    users: [{
        username: "admin",
        password: "<hashat lösenord här>",
        permissions: "*"
    }]
},

httpAdminRoot: '/admin',
```

Hasha lösenordet med:

```
$ node-red admin hash-pw
```

All info [här](https://nodered.org/docs/user-guide/runtime/securing-node-red#editor--admin-api-security) och en video [här](https://www.youtube.com/watch?v=o-iAHZ4jvps) (från ~6 min).

```
# starta (som systemd-tjänst efter installationsskriptet)
$ sudo node-red-start

# eller manuellt i förgrunden
$ node-red
```

Nedan ett exempel på hur "koden" i Node-RED kan se ut, ett så kallat "flow", se mer [här](https://codeberg.org/loralarm/node-red-applications).

<!-- Lägg node-red.png i static/images/ och ta bort kommentaren nedan.
![Node-RED flow](/images/node-red.png) -->

### Uppgradering

- Kör installationsskriptet igen – det uppdaterar både Node.js och Node-RED. Mer info finns också på denna [sida](https://tronics.life/updating-node-red-npm-and-node-js/).

## ntfy

### Installation

- För mer info, [se här](https://docs.ntfy.sh/install/).

```
$ sudo mkdir -p /etc/apt/keyrings
$ curl -fsSL https://archive.heckel.io/apt/pubkey.txt | sudo gpg --dearmor -o /etc/apt/keyrings/archive.heckel.io.gpg
$ sudo apt install apt-transport-https
$ sudo sh -c "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/archive.heckel.io.gpg] https://archive.heckel.io/apt debian main' \
    > /etc/apt/sources.list.d/archive.heckel.io.list"
$ sudo apt update
$ sudo apt install ntfy
$ sudo systemctl enable ntfy
$ sudo systemctl start ntfy
```

### Konfiguration

- https://docs.ntfy.sh/config/

```
$ sudo nano /etc/ntfy/server.yml

...
base-url: "https://exemple.domän.se"

# För aktivering av ios support behövs nedanstående också.
upstream-base-url: "https://ntfy.sh"
```

## Reverse Proxy

<!-- TODO: beskriv t.ex. nginx eller Caddy med certifikat via Let's Encrypt. Caddy är idag det enklaste alternativet (automatiska certifikat). -->

## Gateway

<!-- TODO: konfiguration av gateway/basenhet. -->
