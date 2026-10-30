---
title: "Installation"
---

> **[Documentation work in progress]**

#### Mjukvarustacken för backend/server består idag av:

- [ChirpStack](https://www.chirpstack.io/), "LoRaWAN Network Server"
- [Node-RED](https://nodered.org/), för att analysera och visa data via ett webbinterface.
- [ntfy](https://ntfy.sh/), för att ta emot pushnotiser på android och dator.
- [Pushover](https://pushover.net/), för att ta emot pushnotiser på framför allt Apples produkter (ntfy är lite instabilt på iOS för tillfället). Notera att denna app är en proprietär programvara!

### Server installation

- Installation och konfiguration kommer att ske på en och samma maskin med Ubuntu 20.04 i denna beskrivning.
- Testat på en VM, men ska inte vara något problem på ex. en Raspberry Pi.
- Sätt upp en användare utöver root för all installationer.

```
# logga in som root

# Uppdatera systemet
$ apt update && apt upgrade -y

# Lägg till användare som "super user"
$ adduser <användarnamn>
$ usermod -aG sudo <användarnamn igen>

$ reboot
```

### ChirpStack

#### Installation

- Vi följer [denna](https://www.chirpstack.io/docs/getting-started/debian-ubuntu.html) instruktion.

#### Konfiguration

```
$ sudo nano /etc/chirpstack/chirpstack.toml

[integration]
  enabled = ["mqtt"]

  [integration.mqtt]
    server = "tcp://localhost:1883/"
    json = true
```

```
# starta chirpstack automatiskt vid upstart
sudo systemctl enable chirpstack

# starta chirpstack
sudo systemctl start chirpstack
```

---

### Node-RED

#### Installation

- I detta exempel installerar vi med npm, [se här](https://nodered.org/docs/getting-started/local).

```
$ sudo apt install nodejs
$ sudo npm install -g --unsafe-perm node-red
```

#### Konfiguration

```
# Sätt upp en admin om sidan ska visas publikt!
$ sudo nano .node-red/settings.js

  /** To password protect the Node-RED editor and admin API, the following
     * property can be used. See https://nodered.org/docs/security.html for details.
     */
    adminAuth: {
        type: "credentials",
        users: [{
            username: "admin",
            password: " hashat lösenord här ",
            permissions: "*"
        }]
    },

      /** By default, the Node-RED UI is available at http://localhost:1880/
     * The following property can be used to specify a different root path.
     * If set to false, this is disabled.
     */
    httpAdminRoot: '/admin',
```

All info [här](https://nodered.org/docs/user-guide/runtime/securing-node-red#editor--admin-api-security) och en video [här](https://www.youtube.com/watch?v=o-iAHZ4jvps) (från ~6 min).

```
# starta
$ node-red
```

Nedan ett exempel på hur "koden" i node-red kan se ut, ett så kallad "flow", se mer [här](https://codeberg.org/loralarm/node-red-applications).

<!-- Lägg node-red.png i static/images/ och ta bort kommentaren nedan.
![Node-RED flow](/images/node-red.png) -->

#### Upgradering

- Bra info på denna [sida](https://tronics.life/updating-node-red-npm-and-node-js/)

### ntfy

#### Installation

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

#### Konfiguration

- https://docs.ntfy.sh/config/

```
$ sudo nano /etc/ntfy/server.yml

...
base-url: "https://exemple.domän.se"

# För aktivering av ios support behövs nedanstående också.
upstream-base-url: "https://ntfy.sh"
```

### Reverse Proxy

#### Alternativ

### Gateway

#### Konfiguration
