---
title: "LoRaLarm – ett öppet och fritt larmsystem, en IoT-lösning"
date: 2026-06-12
---

*(Senaste uppdatering 2026-06-12)*

LoRaLarm byggs av personer på deras fritid på öppen och fri mjukvara (och hårdvara) utan vinstsyfte, dvs vem som helst kan sätta upp systemet och testa, granska koden och hjälpa till med förbättringar. Några fördelar med detta är låg kostnad, designflexibilitet, transparens och samarbete.

Mer om [öppen källkod](https://sv.wikipedia.org/wiki/%C3%96ppen_k%C3%A4llkod) och [IoT](https://sv.wikipedia.org/wiki/Sakernas_internet) här.

## Lite fakta om projektet

- Projektet startade vintern 2023–2024.
- Projektet är under aktiv utveckling 2026.
- Under 2024 genomfördes framgångsrika fälttester av ett nytt proaktivt stöldskyddssystem i en båtklubb. Systemet har nu gått in i produktionsfasen.
- Systemet bygger på LoRa-enheter samt en eller flera gemensamma basenheter.
- Överföringen mellan LoRa-enheter och basenheten sker med radio.
- Räckvidden är från några hundra meter till flera kilometer beroende på förhållanden.
- LoRa-enheter kan gå på batteridrift under långa perioder utan batteribyte eller laddning (typiskt flera år).
- Ett git-repo finns nu här. <!-- TODO: länka till projektets git-repo -->
- Ny adress till hemsidan – [loralarm.org](https://loralarm.org)

![Gateway (basenhet) och några olika LoRa-enheter](/images/sensorgateway.jpg)
![Sensormontering på utombordare](/images/montagemotor.jpg)

## Vad försöker LoRaLarm att lösa?

En komplett uppsättning av hårdvara och mjukvara för att enkelt kunna sätta upp ett IoT-nätverk med LoRaWAN som kan användas till olika typer av övervakning och styrning, utan beroende av kommersiella lösningar.

## Vad är LoRaWAN?

LoRaWAN är ett långdistans, lågenergi kommunikationsnätverk som används för Internet of Things (IoT). Här är en kort beskrivning av de viktigaste komponenterna:

![Arkitektur](/images/arkitektur.png)

### Sensorer

LoRaWAN-sensorer, även kallade noder eller slutenheter, samlar in data från omgivningen. Dessa kan mäta allt från temperatur och luftfuktighet till rörelse och vattennivåer. Kostnaden för en LoRa-enhet är från ca 300 kr.

#### LoRa-enheten (sensorn) kan bestå av följande:

- LoRa-modul (på 868 MHz)
- Någon form av sensor, t.ex. temperatur eller rörelse
- Programvara
- Batteri
- Apparatlåda

### Basenhet (Gateway)

Basenheten fungerar som en brygga mellan sensorerna och nätverksservern. Den tar emot radiosignaler från sensorerna och vidarebefordrar dem till servern via internet (Ethernet, WiFi eller mobilnät). En enda basenhet kan hantera tusentals sensorer och täcka stora områden, upp till ca 15 km beroende på miljön. Kostnaden för en enhet är från ca 1500 kr.

### Server

Nätverksservern är hjärtat i LoRaWAN-systemet. Den hanterar all datakommunikation, säkerhet och nätverksoptimering. Servern tar emot data från basenheterna, bearbetar den och skickar den vidare till applikationsservern.

### Användare

Slutanvändarna får tillgång till den bearbetade datan via applikationsservern. Detta kan ske genom ett webbgränssnitt, en mobilapp eller andra plattformar beroende på den specifika tillämpningen. Användarna kan övervaka, analysera och agera på informationen för att optimera olika processer inom exempelvis smarta städer, industri eller jordbruk. Systemet möjliggör effektiv övervakning och styrning av stora IoT-nätverk med låg energiförbrukning och lång räckvidd, vilket gör det idealiskt för många moderna tillämpningar.

## Enkelt att komma igång med LoRaLarm för slutanvändare

### Steg 1: Aktivera och installera

Börja med att aktivera LoRa-enheten hos basenheten. Detta är startpunkten för ditt smarta övervakningssystem. Montera sedan LoRa-enheten strategiskt för optimal täckning.

### Steg 2: Realtidsövervakning

Få direkt tillgång till viktig information genom:

- **Pushnotiser:** Motta larm och data direkt på din mobil eller dator. Detta möjliggör snabba reaktioner på kritiska händelser.
- **Statuspanel:** Övervaka systemet i realtid via en intuitiv "dashboard". Visualisera data från olika sensorer för att få en helhetsbild av situationen.

![Statuspanel](/images/statuspanel01.png)

![Pushnotiser på telefon](/images/pushnotiser2.jpg)
![Pushnotiser på telefon](/images/pushnotiser.png)
![Pushnotiser på telefon](/images/screenshot20260612171005.png)

Pushnotiser på telefon ovan.

### Fler möjligheter…

LoRa-teknologin är tillgänglig för allt från hobbyentusiaster till små företag och större organisationer, vilket möjliggör innovativa lösningar inom en mängd olika tillämpningsområden:

- Skapa ett eller flera större områden med fler basenheter/gateways.
- Integrera med The Things Network.
- Integrera med Home Assistant.
- Med en portabel basenhet kan man använda enheter under t.ex. en resa.
- Valet av sensorer är väldigt flexibelt, allt från temperatur till GPS-övervakning är möjligt.
- Övervaka bil, båt, cykel, utombordare, dörrar och fönster, vattenläckage, trädgård (bevattning), strömavbrott, brand, batteristatus, temperatur, källarföråd, elmätare m.m.
- Styr extern utrustning, såsom belysning, kylskåp, fläktar, värme m.m.

Vill du veta mer eller hjälpa till? Kontakt via e-post. <!-- TODO: lägg till e-postadress/länk -->
