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
