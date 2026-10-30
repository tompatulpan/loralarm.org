#!/usr/bin/env bash
# Skapar Hugo-webbplatsen och monorepo-strukturen för loralarm.org.
# Kör det inifrån roten av ditt klonade repo:  bash init-loralarm-site.sh
set -euo pipefail

[ -f README.md ] || { echo "Kör skriptet inifrån roten av klonat repo (hittar ingen README.md)"; exit 1; }

mkdir -p site/content site/layouts/_default site/layouts/partials site/static/css site/static/images client server

# ---------------------------------------------------------------- README (root)
cat > README.md <<'EOF'
# loralarm.org

Hemvist för hela LoRaLarm-projektet: webbplats, klient- och serverkod samlade i samma repo.

## Struktur

- `site/` – källkoden till webbplatsen [loralarm.org](https://loralarm.org), byggd med Hugo och publicerad på Cloudflare Pages (se `site/README.md`)
- `client/` – kod för LoRa-klienter/sensorer (firmware, hårdvarudesign)
- `server/` – kod för basenheter/gateways och nätverksserver

Webbplatsen byggs automatiskt på Cloudflare Pages vid varje `git push` – se `site/README.md` för redigerings arbetsflödet och build-inställningar.
EOF

# ------------------------------------------------------------------ .gitignore
cat > .gitignore <<'EOF'
# Hugo build output
public/
resources/
.hugo_build_lock
EOF

# --------------------------------------------------------------- site/README.md
cat > site/README.md <<'EOF'
# site/ – loralarm.org (Hugo)

Källkoden till [loralarm.org](https://loralarm.org) – en Hugo-webbplats som byggs och publiceras automatiskt på Cloudflare Pages vid varje `git push`.

## Redigera webbplatsen

1. Installera [Hugo](https://gohugo.io/installation/) (extended behövs ej – ingen SCSS).
2. Kör `hugo server` i `site/` – förhandsgranska på http://localhost:1313
3. Redigera sidtexten i `content/_index.md` (Markdown).
4. `git add`, `git commit`, `git push` – Cloudflare Pages bygger och publicerar automatiskt.

## Bilder

Lägg bildfiler i `static/images/` och referera dem i Markdown som `/images/filnamn.jpg`.

## Struktur

- `content/` – sidtext (Markdown)
- `layouts/` – mallar (Go templates)
- `static/` – bilder och andra statiska filer
- `hugo.toml` – webbplatskonfiguration

## Cloudflare Pages-inställningar

| Inställning | Värde |
|---|---|
| Root directory | `site` |
| Framework preset | Hugo |
| Build command | `hugo --minify` |
| Build output directory | `public` |
| Miljövariabel | `HUGO_VERSION = 0.168.0` |

Sätt `HUGO_VERSION` under Settings → Environment variables, i både Production- och Preview-miljön.

## Kommande steg (innehållsplan)

- [ ] Fyll i TODO-markörerna i `content/_index.md` (git-repo-länk, e-post, bilder)
- [ ] Nyhetssektion (`content/news/` – listmallen finns redan)
- [ ] Engelsk version (multi-language: lägg `[languages.en]` i `hugo.toml` och `_index.en.md`)
- [ ] Docs/guider (`content/docs/`)
EOF

# ------------------------------------------------------------------- hugo.toml
cat > site/hugo.toml <<'EOF'
baseURL = "https://loralarm.org/"
languageCode = "sv-SE"
defaultContentLanguage = "sv"
title = "LoRaLarm"

[params]
  description = "Ett öppet och fritt larmsystem – en IoT-lösning byggt på LoRaWAN."
EOF

# ------------------------------------------------------------ content/_index.md
cat > site/content/_index.md <<'EOF'
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

![Gateway (basenhet) och några olika LoRa-enheter: mönsterkort för sensor samt sensormontering på utombordare](/images/gateway-och-lora-enheter.jpg)

<!-- TODO: kopiera bildfilerna från den gamla exporten (~/migration/loralarm.org på blue) till site/static/images/ och justera filnamnen -->

## Vad försöker LoRaLarm att lösa?

En komplett uppsättning av hårdvara och mjukvara för att enkelt kunna sätta upp ett IoT-nätverk med LoRaWAN som kan användas till olika typer av övervakning och styrning, utan beroende av kommersiella lösningar.

## Vad är LoRaWAN?

LoRaWAN är ett långdistans, lågenergi kommunikationsnätverk som används för Internet of Things (IoT). Här är en kort beskrivning av de viktigaste komponenterna:

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

![Pushnotiser på telefon](/images/pushnotiser.png)

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
EOF

# ------------------------------------------------------------------- layouts
cat > site/layouts/_default/baseof.html <<'EOF'
<!DOCTYPE html>
<html lang="{{ .Site.Language.Lang }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ if .IsHome }}{{ .Title }}{{ else }}{{ .Title }} | {{ .Site.Title }}{{ end }}</title>
  <meta name="description" content="{{ .Site.Params.description }}">
  <link rel="stylesheet" href="{{ "css/style.css" | relURL }}">
</head>
<body>
  {{ partial "header.html" . }}
  <main>
    {{ block "main" . }}{{ end }}
  </main>
  {{ partial "footer.html" . }}
</body>
</html>
EOF

cat > site/layouts/index.html <<'EOF'
{{ define "main" }}
<div class="wrap">
  <article>
    {{ .Content }}
  </article>
</div>
{{ end }}
EOF

cat > site/layouts/_default/single.html <<'EOF'
{{ define "main" }}
<div class="wrap">
  <article>
    <h1>{{ .Title }}</h1>
    {{ .Content }}
  </article>
</div>
{{ end }}
EOF

cat > site/layouts/_default/list.html <<'EOF'
{{ define "main" }}
<div class="wrap">
  <h1>{{ .Title }}</h1>
  <ul class="list">
    {{ range .Pages }}
    <li>
      <a href="{{ .RelPermalink }}">{{ .Title }}</a>
      <time>{{ .Date.Format "2006-01-02" }}</time>
    </li>
    {{ end }}
  </ul>
</div>
{{ end }}
EOF

cat > site/layouts/partials/header.html <<'EOF'
<header>
  <div class="wrap">
    <a class="brand" href="{{ "" | relURL }}">LoRaLarm</a>
    <nav>
      {{ range .Site.Menus.main }}
      <a href="{{ .URL }}">{{ .Name }}</a>
      {{ end }}
    </nav>
  </div>
</header>
EOF

cat > site/layouts/partials/footer.html <<'EOF'
<footer>
  <div class="wrap">
    <p>LoRaLarm – byggs på fritid med öppen källkod. Webbplatsen byggd med Hugo och publicerad via Cloudflare Pages.</p>
  </div>
</footer>
EOF

# ------------------------------------------------------------------- CSS
cat > site/static/css/style.css <<'EOF'
:root {
  --fg: #1f2933;
  --bg: #f8fafc;
  --card: #ffffff;
  --accent: #0f766e;
  --muted: #64748b;
  --border: #e2e8f0;
  --max: 46rem;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  color: var(--fg);
  background: var(--bg);
  line-height: 1.65;
}

.wrap { max-width: var(--max); margin: 0 auto; padding: 0 1.25rem; }

header { background: var(--card); border-bottom: 1px solid var(--border); }
header .wrap { display: flex; align-items: center; justify-content: space-between; padding: 0.8rem 1.25rem; }
.brand { font-weight: 700; font-size: 1.15rem; color: var(--accent); text-decoration: none; }
nav a { margin-left: 1rem; color: var(--fg); text-decoration: none; }
nav a:hover { color: var(--accent); }

main { padding: 1.5rem 0 3rem; }

h1 { font-size: 1.7rem; line-height: 1.3; }
h2 { margin-top: 2.2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.3rem; }
h3 { margin-top: 1.6rem; }

a { color: var(--accent); }

img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1rem auto;
  border-radius: 8px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
}

footer { background: var(--card); border-top: 1px solid var(--border); color: var(--muted); font-size: 0.9rem; }
footer .wrap { padding: 1rem 1.25rem; }

.list { list-style: none; padding: 0; }
.list li { display: flex; justify-content: space-between; gap: 1rem; padding: 0.4rem 0; border-bottom: 1px solid var(--border); }
EOF

# --------------------------------------------------------- client/ & server/
cat > client/README.md <<'EOF'
# client/

Kod för LoRaLarm-klienter (sensorer/slutenheter): firmware, sensorprogramvara och hårdvarudesign (mönsterkort).

## Planerat innehåll

- Firmware för LoRa-sensorer (868 MHz)
- Sensordefinitioner och montagebeskrivningar
- Mönsterkort-filer

*TODO: fyll på när koden flyttas in hit.*
EOF

cat > server/README.md <<'EOF'
# server/

Kod för LoRaLarm-serverdelen: basenheter/gateways, nätverksserver och integrationslager.

Se även kunskapsbasen kring ChirpStack/Mosquitto-setup på "lora"-boxen för referensarkitekturen.

*TODO: fyll på när koden flyttas in hit.*
EOF

echo
echo "Klart! Alla filer skapade. Nästa steg:"
echo "  git add -A"
echo "  git commit -m 'Add Hugo site under site/ + monorepo structure'"
echo "  git push"
