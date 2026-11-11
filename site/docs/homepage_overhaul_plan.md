# Homepage overhaul — plan

Working doc for rebuilding the homepage from `site/docs/dark_mockup.html`, with
dual language from the start (per `site/docs/language.md`).

## 0. Decisions (locked with Tomas)

| Topic | Decision |
|---|---|
| Theme | **Dark only** — dark mockup tokens replace the current light theme site-wide |
| Hero status panel | **Demo mode confirmed (Tomas)** — localized demo data from `data/statusdemo.json`; live data from the backend is a future follow-up that waits on the PWA API (see §4) |
| Current long-form homepage | **Moves to an About page** (`/om/`, `/en/about/`); homepage becomes the mockup's compact landing page |
| Language split | Per `language.md`: `sv` at `/`, `en` at `/en/` |

## 1. i18n setup (do this first — everything else builds on it)

**hugo.toml**
- Merge the `language.md` block: `defaultContentLanguage = "sv"`,
  `defaultContentLanguageInSubdir = false`, `[languages.sv]` / `[languages.en]`.
- Menus move **inside each language block**. Today `[[menus.main]]` at top level
  is global; with two languages we define it per language so names translate
  (`Installation` ↔ `Installation`, `Testade enheter` ↔ `Tested devices`, …).
- Per-language `title` and `params.description` (today's sv description moves
  under `[languages.sv.params]`; an English counterpart is added).

**UI strings** — `i18n/sv.toml` + `i18n/en.toml` for everything a template renders:
nav labels, `Get started`, hero kicker/lede/button text, section kickers and
headings, legend names, footer strings. Homepage prose lives in the i18n files
(or in `_index*.md` front-matter params — pick i18n files; keeps `_index.md`
minimal).

**Content files**
- `content/_index.md` (sv) + `content/_index.en.md` (en): short front matter +
  params only; all visible copy comes from layouts + i18n.
- Subpages (`installation`, `ntfy`, `pushover`, `testadeenheter`) stay **sv-only
  for now**. Hugo simply won't serve them under `/en/`. Language switcher must
  handle the fallback (see below).
- About pages: `content/om.md` (sv) + `content/om.en.md` (en, translated).

**Language switcher** — new partial `layouts/partials/lang.html`:
- If the current page has a translation (`.Translations`), link there.
- Otherwise link to the other language's homepage (never a 404).
- Replaces the static `EN / SV` chip in the mockup nav; also dropdown "Other"
  column in footer.

**hreflang** — baseof gains `<link rel="alternate" hreflang=…>` from
`.Translations` + x-default, plus `rel="canonical"`. Good for SEO from day one.

## 2. Theme port (dark only)

The mockup is already organized the way our CSS is split, so the port is mostly
**tokens, then components**:

- `static/css/variables.css` — replace light tokens with the mockup `:root`:
  `--bg #0b0f14`, `--bg-panel #11161f`, `--bg-panel-2 #0e131b`, `--line #1d2632`,
  `--text #e6edf3`, `--text-muted #8b98a5`, `--accent #2dd4bf`, `--ok/--warn/--alert`,
  mono + sans stacks (system fonts — no webfont download needed). Also set
  `color-scheme: dark` on `:root`.
- `static/css/base.css` — body, grid background (the 48px blueprint grid),
  headings, links, table defaults.
- `static/css/layout.css` — `.wrap` (max-width 1080 for the homepage hero,
  keep the narrower reading measure for doc pages), nav, hero, sections.
- `static/css/components.css` — panels, LED + signal bars, pills, cards,
  flow nodes, legend, badges, buttons, blink animation.
- Existing subpages (installation/ntfy/pushover/testadeenheter) inherit the
  dark theme through the tokens; do one contrast pass over code blocks
  (already dark — good), galleries, and the lightbox.

## 3. Homepage template

Homepage becomes **template-driven**: `layouts/index.html` renders the mockup's
sections; `_index*.md` supplies only front matter. Sections:

1. **Nav** (rewrite `header.html`): logo with status dot, per-language menu,
   language switcher, `Get started` CTA (→ Installation page for now).
2. **Hero** (2-col grid on ≥860px): kicker, h1, lede, CTAs (Get started /
   View on GitHub), meta row (MIT, runs on your hardware, no cloud required) —
   paired with the **live status panel**.
3. **What it does** — monitor / alert / control cards with inline SVGs.
4. **Architecture** — sensor → gateway → server flow. Mirror the *real* stack
   (our story: sensor → RAK gateway → ChirpStack + Mosquitto → alarm service),
   not the mockup's generic labels.
5. **Design language legend** — OK/WARNING/ALERT/OFFLINE. Matches how the actual
   status panel and PWA color states, so keep the wording consistent with the
   PWA spec when it lands.
6. **Start here** (docs table section) — point at the **real existing pages**
   (Installation, ntfy, Pushover, Testade enheter, Om) instead of the mockup's
   fictional `/docs/nodes/` paths. Add per-row status later as guides split out.
7. **Open-source band** — license/stack badges + contribute copy (GitHub links,
   "translations welcome" — nice once duallang ships).
8. **Footer** (rewrite `footer.html`): brand + tagline, Site column (Docs,
   Om/About, language switch), Project column (GitHub, Issues, License).

Copy: English text comes from the mockup; Swedish is translated to match the
current site's voice. Responsive: single-column collapse at 860px, arrow
rotation in flow, `h1` scale-down.

## 4. Live status panel

- **Source of truth:** the PWA's planned FastAPI `alarms.loralarm.org/api/*`
  reading `Engine.snapshot()` (see `PWA/docs/pwa-design.md` §2). That API does
  **not exist yet**, so this phase has a demo fallback from day one.
- **Client:** small `static/js/status-panel.js`: fetch JSON, render device rows
  in the hero panel (LED class + signal bars from RSSI/SNR, name, state line),
  poll ~60 s, re-fetch on `visibilitychange`. No framework.
- **Fallback:** if fetch fails (or PWA not deployed), render a static demo
  dataset from the i18n file. The panel must never look broken, and the
  Cloudflare Pages **build must not depend on the private API** — always
  client-side.
- **Exposed data — decision needed before implementation** (see §6):
  (a) public summary only (counts per state, no device names),
  (b) real device names publicly readable,
  (c) demo mode until PWA ships and an auth story exists.
- **CORS:** FastAPI must allow origin `https://loralarm.org`.

## 5. Content migration

- Current `content/_index.md` (long article) → `content/om.md` "Om LoRaLarm",
  keeping galleries, LoRaWAN explainer, project facts, step guide. Add
  `/en/about/` translation later or at launch (decide in §6).
- Homepage links to it from the open-source band / a "Läs mer" link.
- Carry over the open TODOs from `_index.md` (email link, GitHub URL) so they
  don't get lost — same TODO markers in `om.md`.

## 6. Open questions (for Tomas)

1. **Status panel exposure:** summary-only (a), real names (b), or demo until
   PWA API lands (c)? Default suggestion: demo mode at launch (c), then summary
   (a) once the API exists — real names (b) publicly without auth is risky.
2. **Nav structure:** keep the current menu (Hem, Installation, ntfy, Pushover,
   Testade enheter) or adopt the mockup's (Overview / Docs / News / GitHub)?
   Suggestion: current menu + GitHub link in nav, mockup-style styling.
3. **About page in English at launch**, or sv-only with graceful switcher
   fallback?
4. Hero headline: use the mockup's English h1 translated, or keep a Swedish
   tone-of-voice headline? (H1 is the biggest voice decision on the page.)

## 7. Implementation phases

Each phase ends with `env -u LD_LIBRARY_PATH hugo` (build) + link check of
touched URLs; no git adding/committing (per AGENTS.md, staging is Tomas's).

| Phase | Scope | Files |
|---|---|---|
| 1 | i18n config + UI strings + switcher partial; site unchanged visually | `hugo.toml`, `i18n/{sv,en}.toml`, `layouts/partials/lang.html`, `header.html` |
| 2 | Dark theme token/component port site-wide | `static/css/{variables,base,layout,components}.css` |
| 3 | Homepage template (static demo panel), footer rewrite, sv+en copy | `layouts/index.html`, `layouts/partials/footer.html`, `content/_index{,.en}.md`, `i18n/*` |
| 4 | About page migration + cleanup of `_index.md` | `content/om.md`, nav/menu updates |
| 5 | Live status panel + JS + endpoint contract with PWA work | `static/js/status-panel.js`, coordination with `PWA/docs/pwa-design.md` |
| 6 (optional) | Translate subpages, news section | `content/*.en.md` etc. |

Phases 1–4 are self-contained and shippable; phase 5 can trail the PWA API.
