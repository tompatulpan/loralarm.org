---
title: "Testade enheter"
---

## Hårdvara som är testad i verkligheten

Alla enheter nedan kör i en riktig LoRaLarm-installation och finns med i projektets
testsviter: deras riktiga upplänkar används som testdata, och deras avkodare
(payload codecs) och familjedefinitioner lever i repot. Det betyder att enheterna
inte bara "borde fungera" – de övervakas av tester vid varje ändring.

### RAK3172 – bygg din egen sensor (WisBlock)

| | |
|---|---|
| **Typ** | DIY-sensornod (RAK3172, STM32WLE5) |
| **Firmware** | `devices/rui3_sensor/` i repot (RAK RUI3, Cayenne LPP) |
| **Batteri** | 3 V-cell, ~24 µA i viloläge – batteritid i år, inte dagar |
| **Sensorer** | RAK1901 (temp/fukt), RAK1902 (tryck), RAK1903 (ljus), RAK1904 (accelerometer/rörelse), RAK1906 (BME680) |
| **Ingångar** | Switch/larminingång på RAK19003/RAK19007-basplattorna |
| **Inställning** | AT-kommandon och downlink (sändintervall, accelerometerkänslighet), sparas i flash |
| **Status** | Testad – växelstatus i varje heartbeat, omedelbar upplänk vid rörelse eller öppnad slinga |

### RAK4631 – samma kort som Meshtastic

| | |
|---|---|
| **Typ** | DIY-larmnod (RAK4631, nRF52840) |
| **Firmware** | `devices/android_rak4631/` i repot (Arduino) |
| **Batteri** | LiPo-cell, ~40 µA i viloläge |
| **Sensorer** | LIS3DH-accelerometer (rörelse/läge), temperatur |
| **Ingångar** | Switch/larminingång (GPIO), lägesrapport i varje sändning |
| **Inställning** | Downlink för accelerometerkänslighet, frekvens och duration |
| **Status** | Testad – extra enkel att prova: kortet är en av de vanligaste Meshtastic-plattformarna, så du kanske redan har ett i byrålådan |

### Dragino LDS02 – färdig dörrkontakt

| | |
|---|---|
| **Typ** | Komplett dörrkontakt (Dragino Switch LDS02) |
| **Batteri** | 2 × AAA (~3,0 V) |
| **Funktion** | Dörrstatus vid varje ändring plus heartbeat; batterispänning i varje sändning |
| **Larm** | Dörrlarm (öppen/stängd) med valfri tidsruta, t.ex. bara nattetid |
| **Status** | Testad – inga temperaturdata, inga downlink-inställningar |

### Browan TBWL100 – färdig vattenläckagesensor

| | |
|---|---|
| **Typ** | Komplett vatten-/läckagesensor (Browan TBWL100) |
| **Batteri** | 3,6 V Li-SOCl2-cell |
| **Funktion** | Läckestatus skickas direkt och upprepas medan det är vått; keep-alive var 60:e minut med temperatur, luftfuktighet och batteri |
| **Larm** | Vattenlarm (läcka upptäckt/torrt igen) |
| **Status** | Testad – ingen downlink-konfiguration; batterigränser är kalibreringsbara per enhet |

---

## Komplettera en WisBlock-nod

DIY-firmwaren (`rui3_sensor`) stöder följande RAK-moduler – plocka de sensorer
din applikation behöver:

| Modul | Funktion | Fack |
|---|---|---|
| RAK1901 | Temperatur & luftfuktighet (SHTC3) | Valfri I²C |
| RAK1902 | Lufttryck (LPS35HW / LPS22HB) | Valfri I²C |
| RAK1903 | Omgivningsljus (OPT3001) | Valfri I²C |
| RAK1904 | Accelerometer / rörelse (LIS3DH) | Fack D (INT1 → WB_IO5) |
| RAK1906 | Miljö (BME680): temp, fukt, tryck, gas | Valfri I²C |
| RAK19003 | Mini-basplatta med switch/larmingång | — |
| RAK19007 | Stor basplatta med switch/larmingång | — |

## Tredjepartsenheter

Systemet är inte låst till enheterna ovan. Servern är datastyrd: en ny sensortyp
beskrivs i en enda JSON-fil (familjedefinition) plus en payload codec i
ChirpStack – ingen serverkod behöver ändras. Se `server/python/README.md` i
repot för hur en enhet läggs till.
