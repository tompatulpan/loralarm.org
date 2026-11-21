# Flottraden (sensor-badge) — strukturerad definition

Sanningskälla för hur en enhetsrad ritas i Status-vyn. Gäller **båda teman**
(Standard och Statuspanelsvy): samma innehåll, samma regler, olika skal/typografi.
Referensskiss: `PWA/docs/pwa-mockup-v2.html` (ruta 1 och 2). Klassnamn nedan är
skissens (`row2`/`led`/`nm`/`pill`/`rd`/`sig`).

## Zonkarta

```
         Z0            Z1          Z2: .nm                    Z3: .pill
         ┌3px┐        ┌────┐
RAD 1:   │   │  .led  │  namn  <small>· grupp</small>    <span class="pill …">
         │   │        └────┘
RAD 2:   │   │            .rd — <span class="temp"> · <span class="bat"> ─ <span class="sp"> ─ senast — <span class="sig">
         └───┘                                                                    ▲
                                                                                  Z5 (sista elementet i .rd)
```

Två textrader, alltid. Inget innehåll radbryts någonsin — overflow blir `…` (ellips).

## Zoner

### Z0 — Allvarslist (radens vänsterkant)

**Placering:** ingen egen ruta — det är radens **yttre vänsterkant**, en 3 px bred
vertikal list som sträcker sig över radens hela höjd, ritad *före* Z1 (LED/ikonen).
Implementeras som `border-left: 3px solid` på raden. De 3 px:en är **alltid
reserverade** (listen är transparent i normaltillståndet) — ingenting flyttar sig
när ett larm kommer, raden byter bara färg på plats. Allvarslisten parar ihop
sig med en svag bakgrundston över hela raden; tillsammans syns problemrader
perifert, utan att läsas.

| Tillstånd | Z0-list | Bakgrundston på raden | allt annat som följer med |
|---|---|---|---|
| normal | transparent (osynlig) | ingen | LED grön, pill OK |
| varning | bärnsten | `rgba(251,191,36,.05)` | LED bärnsten, varningspill |
| aktivt larm | röd | `rgba(248,113,113,.06)` | LED röd + blink, larmpill |

```
── NORMAL ────────────────────────────     ─── LARM (samma DOM, andra klasser) ───
<div class="row2">                         <div class="row2 alarm-r">
│ ┊  border-left: 3px transparent          █   border-left: 3px var(--red)
│ ┊  (3 px alltid reserverade —            █   background: rgba(248,113,113,.06)
│ ┊   inget flyttar sig)                   █
│ ●  .led                → grön            █ ● .led            → röd, blink 1.4 s
│    .nm + small         → oförändrad      █   .nm + small      → oförändrad
│    .pill               → .pill.ok  "OK"  █   .pill            → .pill.alarm "TILTLARM"
│    .rd:                →                 █   .rd:             →
│      .temp · .bat      → muted/teal      █     .temp · .bat   → oförändrad
│      .sp  (mellanrum)                   █     .sp
│      senast-span        → muted          █     senast-span    → muted
│      .sig i.on ×N      → staplar        █     .sig i.on ×N   → staplar
</div>                                     </div>

Varning heter .warn-r (bärnsten list + .05-ton, .pill.warn); avslagen
enhet heter .dim (raden opacity .6, .led grå, .pill.off).
```

I Standard-temat (separata kort) ritas listen på kortets vänsterkant; i
Statuspanelsvyn på radkanten inne i den gemensamma panelen.

### Z1 — Statusindikator (den vänstra "ikonen")

| | Standard | Statuspanelsvy |
|---|---|---|
| Element | ikonruta 34×34, kind-ikon (Lucide): sensor=disk, door=dörr, water=droppe, button=cirkel | glödande LED, 9–10 px |
| Bakgrund | `#1c2430`, radie 9 px | ingen (glöd via box-shadow) |
| Färg | `currentColor` (tematext) | värsta tillståndet: **ok** grön · **warn** bärnsten · **alarm** röd + blink 1.4 s · **av** grå `#3a4654` |
| Vertikalt | centrerad över rad 1–2 | i linje med namnet (rad 1) |

### Z2 — Identitet (rad 1 vänster)

| Element | Innehåll | Källa (API) |
|---|---|---|
| namn | enhetsnamn, mono i statuspanelsvyn, sans (fetare) i standard | `name` |
| grupp | `· grupp` i mindre/muted grad, **endast om gruppen finns** | `group` |
|följs ej | `· följs ej` i grad, endast när användaren inte följer | `following` |
| pinstallation | namnet trunkeras **först** med `…`; grupp/följs-ejklämmer döljs hellre än att namnet klipps | — |

**Utbytt i v2:** "🛡 larm på"-chippet försvinner (på är normaltillståndet). Avslagen
tid visas i pillen (Z3) i stället. family-rad (RAD 3 i gamla layouten) försvinner
— grupp ligger här.

### Z3 — Statuspill (rad 1 höger)

Prioritet (första träff gäller):

| Prioritet | Pill | Färg | Källa |
|---|---|---|---|
| 1 | aktivt larm: etiketten versal, t.ex. `DÖRRLARM`, `TILTLARM` | röd | `active[]` + `state.alarms` |
| 2 | nyligen stängt larm (puls): etikett + `· tid` | röd | `state.recent[]` |
| 3 | `INGEN KONTAKT` (stale-larm aktivt) | röd | `active[]: "stale"` |
| 4 | avslaget larm: `AV · HH:MM` med bell-off-ikon i statuspanelsvyn | muted/grå | `armed: false` + `armed_until` |
| 5 | annars `OK` | grön | — |

`white-space: nowrap`, `max-width` med ellips som sista utväg. Vid avslagen
enhet utan `armed_until` (admin "tills vidare"): bara `AV`.

### Z4 — Mätvärden (rad 2)

Vänstergrupperat: `temp` `·` `batteri` — därefter flex-mellanrum — `senast sedd`.

| Element | Format | Färg | Källa |
|---|---|---|---|
| temperatur | `3.8°C` (en decimal, ingen mellanrum mellan värde och enhet); `—` om saknas | teal (statuspanel) / blå (standard); färg bara som signalvärde | `temperature` |
| batteri | `[batt-ikon] 2.94V · 57%` — ikon (Lucide battery, 14 px) framför; procenten alltid med när den finns. `—` om inget batteri rapporteras | muted normalt; **bärnsten när `battery_pct < 20`**; röd vid `battery_empty` | `battery_voltage`, `battery_pct` |
| senast sedd | `1 min` · `5 min` · `2 h` · `3 d` (kortformer; aldrig radbrutet) | muted normalt; **röd när > 24 h** | `last_seen` |
| färgprincip | "Färg bara när något är fel" — normala värden ska inte skrika | | |

### Z5 — Signal (rad 2 höger)

4 staplar, 4 px breda, höjder 5/8/11/14 px.

| Regel | Värde |
|---|---|
| antal tända staplar (RSSI) | ≥ −60 → 4 · ≥ −80 → 3 · ≥ −100 → 2 · annars 1 |
| färg (SNR, falls worst) | `snr ≥ 5` teal/grön · `snr ≥ 0` bärnsten · `snr < 0` röd (RSSI-trösklar när SNR saknas: ≥ −80 ok, ≥ −100 warn) |
| släckta staplar | linjefärg |
| tooltip | `RSSI −62dBm · SNR 9.1dB` (hover/dösen) |
| ikon saknas | ingen signal-mätare alls när `rssi: null` (elementet renderas inte) |

## Radnivå

| Regel | Värde |
|---|---|
| höjd | exakt 2 textrader (~64 px totalt), `padding 11px 16px` |
| dim | avslagen enhet: hela raden `opacity .6` **och** Z1-grå LED |
| sortering | 1) rader med larm/varning först 2) därefter namn (v2: "larm först") |
| tryckyta | hela raden ≥ 44 px, navigerar till enhetsdetaljen |

## Temaskillnader — sammanfattning

| Zon | Standard | Statuspanelsvy |
|---|---|---|
| Z1 | ikonruta 34×34 | LED 9 px, tillståndsfärgad (röd = blink) |
| Z2 namn | sans, 600, .82rem | mono, 400, 13 px |
| Z3 pill | fylld pill (grön/röd/… solidare) | konturpill med tonad bakgrund, mono 10–11 px |
| Z4 färgsignaler | blå temp, bärnsten-lågt-batteri | teal temp, bärnsten-lågt-batteri |
| Z5 staplar | 3 px breda, grön/gul/röd | 4 px breda, teal/bärnsten/röd |
| bakgrund | separata kort med ruta/kant | raderna ligger i ETT ultrapanel (`#fleet`), skiljs av hairlines |
| Z0 | (v2) kantlist på kortkanten | kantlist på radkanten, synligare mot panelen |

## Datakällor

Allt från `GET /api/devices` (per rad) + `GET /api/state` (global, för recent):
`name, family, kind, group, temperature, battery_voltage, battery_pct, rssi,
snr, last_seen, armed, armed_until, active[], following`.
