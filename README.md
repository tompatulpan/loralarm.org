# loralarm.org

Hemvist för hela LoRaLarm-projektet: webbplats, klient- och serverkod samlade i samma repo.

## Struktur

- `site/` – källkoden till webbplatsen [loralarm.org](https://loralarm.org), byggd med Hugo och publicerad på Cloudflare Pages (se `site/README.md`)
- `client/` – kod för LoRa-klienter/sensorer (firmware, hårdvarudesign)
- `server/` – kod för basenheter/gateways och nätverksserver

Webbplatsen byggs automatiskt på Cloudflare Pages vid varje `git push` – se `site/README.md` för redigerings arbetsflödet och build-inställningar.
