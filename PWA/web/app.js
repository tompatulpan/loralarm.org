/* LoRaLarm PWA — static frontend, no framework. Talks to the FastAPI app
 * served by the loralarm service (--web --static PWA/web). Every
 * state-changing request carries X-Requested-With: loralarm (CSRF guard). */
'use strict';

const $ = (sel) => document.querySelector(sel);

const KIND_LABEL = {
  temp_low: 'Låg temperatur', temp_high: 'Hög temperatur',
  battery_low: 'Lågt batteri', battery_empty: 'Tomt batteri',
  motion: 'Rörelselarm', circuit: 'Kretslarm', tilt: 'Tiltlarm',
  door: 'Dörrlarm', stale: 'Ingen kontakt', armed: 'Larm',
  button: 'Knapplarm',
};
// Lucide outline icons (ISC License, lucide-icons/lucide), inline to keep the
// CSP (`default-src 'self'`) and offline PWA intact; colored via currentColor.
const KIND_ICON = {
  door: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 21H2"/><path d="M10 3H7a2 2 0 00-2 2v16"/><path d="M14 12h.01"/><path d="M19 21V5a2 2 0 00-1.675-1.974l-6.163-1.013A1 1 0 0010 3v18a1 1 0 001.124.992z"/><path d="M22 21h-3"/></svg>',
  water: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/></svg>',
  button: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="12" r="10"/></svg>',
  sensor: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/></svg>',
};
// Lucide battery (fleet-raden Z4); kontur, färg via currentColor
const BATT_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="10" x="2" y="7" rx="2"/><path d="M22 11v2"/><path d="M6 11v2"/></svg>';
// larm-sorter som ger VARNING (bärnsten) i stället för rött på raden
const WARN_KINDS = new Set(['battery_low', 'battery_empty', 'button_battery', 'battery_shutdown']);

let ME = null;            // {name, role}
let DEVICES = [];         // GET /api/devices
let STATE = { ok: true, alarms: [], recent: [] };   // GET /api/state
let WEBPUSH_ON = false;
let LAST_OK = 0;          // ms timestamp of the last successful refresh
let FAILS = 0;            // consecutive failed refreshes
let SNOOZE_OPEN = null;   // devEUI whose snooze picker is open (survives re-renders)
let SWIPE_OPEN = null;    // 'devEUI/kind' of the alarm banner swiped open (ditto)
let TICKING = false;
let DEFERRED_INSTALL = null;   // beforeinstallprompt event (Chrome/Android et al.)
const POLL_MS = 15000;
const STALE_AFTER_MS = 2.5 * POLL_MS;   // ~2 missed polls
const EUI_RE = /^[0-9a-f]{16}$/;
const THEME_KEY = 'loralarm-theme';   // 'home' = statuspanelsvy (loralarm.org-utseende), annars default

// ---------- API ----------

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: { 'X-Requested-With': 'loralarm' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (r.status === 401) { showLogin(); const e = new Error('ej inloggad'); e.status = 401; throw e; }
  if (!r.ok) { const e = new Error((data && data.detail) || ('HTTP ' + r.status)); e.status = r.status; throw e; }
  return data;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 3500);
}

// ---------- helpers ----------

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function relTime(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just nu';
  const m = s / 60;
  if (m < 60) return `${Math.floor(m)} min sedan`;
  const h = m / 60;
  if (h < 24) return `${Math.floor(h)} h sedan`;
  return `${Math.floor(h / 24)} dagar sedan`;
}

function fmtUntil(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : `till ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function iconFor(d) {
  if (d.kind === 'door') return KIND_ICON.door;
  if (d.kind === 'water') return KIND_ICON.water;
  if (d.kind === 'button') return KIND_ICON.button;
  return KIND_ICON.sensor;
}

function device(eui) { return DEVICES.find((d) => d.dev_eui === eui); }

// The data on screen is only trustworthy while the server keeps answering. When it
// does not (service/tunnel down, phone offline, timers suspended) say so loudly
// instead of leaving a green "no active alarms" banner up.
function isStale() {
  return !!ME && (FAILS >= 2 || Date.now() - LAST_OK > STALE_AFTER_MS);
}

function hhmm(ms) {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// iPadOS 13+ reports a desktop Mac user agent
const IS_IOS = /iPhone|iPad|iPod/.test(navigator.userAgent)
  || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
const IS_ANDROID = /Android/.test(navigator.userAgent);

// running as an installed app (home-screen icon), not in a browser tab
function runningStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.matchMedia('(display-mode: fullscreen)').matches
    || navigator.standalone === true;
}

// ---------- theme (valfritt CSS-lager: statuspanelsvy à la loralarm.org) ----------

function applyTheme() {
  const home = localStorage.getItem(THEME_KEY) === 'home';
  document.body.classList.toggle('theme-home', home);
  const tg = $('#theme-toggle');
  if (tg) tg.classList.toggle('on', home);
}

function toggleTheme() {
  const home = localStorage.getItem(THEME_KEY) !== 'home';
  if (home) localStorage.setItem(THEME_KEY, 'home');
  else localStorage.removeItem(THEME_KEY);
  applyTheme();
  toast(home ? 'Statuspanelsvy på' : 'Standardvy på');
}

let WAS_STALE = false;

function renderConn() {
  const el = $('#conn');
  const stale = isStale();
  el.classList.toggle('hidden', !stale);
  if (stale !== WAS_STALE) {          // staleness can start without a failed poll (frozen timers)
    WAS_STALE = stale;
    if (ME) {
      renderBanners('#banners');
      renderBanners('#dev-banners');
    }
  }
  if (stale) {
    el.innerHTML = `<div class="dot"></div><div><div class="t">Ingen kontakt med servern</div>
      <div class="d">Visar data från ${esc(hhmm(LAST_OK))}. Aktiva larm kan saknas — lita inte på statusen.</div></div>`;
  }
}

// ---------- views ----------

function showLogin() {
  ME = null;
  $('#view-login').classList.remove('hidden');
  $('#view-app').classList.add('hidden');
  $('#login-token').focus();
  renderInstall();
}

function showApp() {
  $('#view-login').classList.add('hidden');
  $('#view-app').classList.remove('hidden');
}

function route() {
  const h = location.hash || '#/';
  const devMatch = h.match(/^#\/dev\/([0-9a-f]{16})$/);
  const onSettings = h === '#/settings';
  if (!devMatch || devMatch[1] !== SNOOZE_OPEN) SNOOZE_OPEN = null;
  $('#view-fleet').classList.toggle('hidden', !!devMatch || onSettings);
  $('#view-device').classList.toggle('hidden', !devMatch);
  $('#view-settings').classList.toggle('hidden', !onSettings);
  $('#tab-fleet').classList.toggle('on', !onSettings);
  $('#tab-settings').classList.toggle('on', onSettings);
  $('#appbar-title').textContent = devMatch ? device(devMatch[1])?.name || 'Enhet' : 'LoRaLarm';
  if (devMatch) renderDevice(devMatch[1]);
  else if (onSettings) renderSettings();
  else renderFleet();          // the fleet view is not re-rendered while hidden: refresh it on return
  renderChrome();
  renderConn();
}

function renderChrome() {
  const n = STATE.alarms.length;
  const r = (STATE.recent || []).length;
  const badge = $('#alarm-badge');
  badge.classList.toggle('hidden', n + r === 0);
  badge.textContent = n > 0 ? `${n} larm aktiv${n > 1 ? 'a' : ''}` : (r > 0 ? `${r} nytt larm` : '');
}

// "Sweep left to delete": every alarm banner (active + recent pulse) can be
// swiped left to reveal a dismiss button. The dismissal is per-user state on
// the server (POST /api/alarms/ack): the badge stays away across polls,
// reloads and this phone — and returns as soon as the alarm fires again.
// SWIPE_W must match the .swipe-del width in styles.css.
const SWIPE_W = 84;
let SWIPE_DRAGGING = false;

function bannerKey(a) { return `${a.dev_eui}/${a.kind}`; }

function closeOpenSwipe(except) {
  document.querySelectorAll('.swipe-item.open').forEach((el) => {
    if (el !== except) el.classList.remove('open');
  });
}

function renderBanners(el) {
  if (SWIPE_DRAGGING) return;           // a poll must not replace the row under the finger
  const host = $(el);
  const recent = STATE.recent || [];
  if (!STATE.alarms.length && !recent.length) {
    host.innerHTML = isStale() ? '' : `<div class="banner ok"><div class="dot"></div>
      <div><div class="t">Inga aktiva larm</div><div class="d">Alla enheter är tysta</div></div></div>`;
    return;
  }
  const rows = [
    ...STATE.alarms.map((a) => ({ a, cls: '', when: '' })),
    ...recent.map((a) => ({ a, cls: 'recent', when: ` · ${esc(relTime(a.ts))}` })),
  ];
  host.innerHTML = rows.map(({ a, cls, when }) => `
    <div class="swipe">
      <button class="swipe-del" type="button" aria-label="Ta bort larmet">Ta bort</button>
      <div class="banner swipe-item${cls ? ' ' + cls : ''}${SWIPE_OPEN === bannerKey(a) ? ' open' : ''}">
        <div class="dot"></div>
        <div>
          <div class="t">${esc(KIND_LABEL[a.kind] || a.kind)}: ${esc(a.name)}${when}</div>
          <div class="d">${esc(a.detail)}</div>
        </div>
      </div>
    </div>`).join('');
  host.querySelectorAll('.swipe').forEach((row, i) => wireSwipe(row, rows[i].a));
}

function wireSwipe(row, a) {
  const item = row.querySelector('.swipe-item');
  if (!item) return;                    // insurance: never abort the whole fleet
  const key = bannerKey(a);
  row.querySelector('.swipe-del').addEventListener('click', () => dismissAlarm(a));

  let x0 = 0, base = 0, offset = 0, dragging = false;

  item.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (SWIPE_OPEN !== key) { closeOpenSwipe(item); SWIPE_OPEN = null; }
    dragging = SWIPE_DRAGGING = true;
    x0 = e.clientX;
    base = offset = SWIPE_OPEN === key ? -SWIPE_W : 0;
    item.classList.add('drag');          // no transition while the finger is down
    try { item.setPointerCapture(e.pointerId); } catch (_) { /* pointercancel still ends it */ }
  });
  item.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    offset = Math.max(-SWIPE_W, Math.min(0, base + e.clientX - x0));
    item.style.transform = offset ? `translateX(${offset}px)` : '';
  });
  const end = () => {
    if (!dragging) return;
    dragging = SWIPE_DRAGGING = false;
    item.classList.remove('drag');
    item.style.transform = '';
    if (Math.abs(offset - base) < 8) {   // a tap (no drag): an open row closes
      if (SWIPE_OPEN === key) { SWIPE_OPEN = null; item.classList.remove('open'); }
      return;
    }
    const open = offset <= -SWIPE_W / 2;
    SWIPE_OPEN = open ? key : null;
    item.classList.toggle('open', open);
  };
  item.addEventListener('pointerup', end);
  item.addEventListener('pointercancel', end);
}

async function dismissAlarm(a) {
  try {
    await api('/api/alarms/ack', { method: 'POST', body: { dev_eui: a.dev_eui, kind: a.kind } });
    SWIPE_OPEN = null;
    toast('Larmet dolt — visas igen när det larmar på nytt');
    await refresh();
    renderChrome();
    route();                            // re-renders the visible view, banners included
  } catch (e) { toast(e.message); }
}

function recentFor(d) { return (STATE.recent || []).filter((r) => r.dev_eui === d.dev_eui); }

function pillFor(d) {
  const rc = recentFor(d);
  if (!(d.active && d.active.length) && rc.length) {
    return `<span class="pill alarm">${rc.map((r) => esc(KIND_LABEL[r.kind] || r.kind)).join(', ').toUpperCase()} · ${esc(relAge(rc[0].ts).text)}</span>`;
  }
  if (d.active && d.active.length) {
    return `<span class="pill ${WARN_KINDS.has(d.active[0]) ? 'warn' : 'alarm'}">${d.active.map((k) => esc(KIND_LABEL[k] || k)).join(', ').toUpperCase()}</span>`;
  }
  if (!d.armed) {                      // AV · HH:MM (v2: pillen, inte något chip)
    // "tills vidare" (admin) har ingen armed_until: bara "AV". OBS: isNaN(null)
    // är falskt (Number(null)===0), så null-testet måste komma explicit först
    const u = d.armed_until ? new Date(d.armed_until) : null;
    const until = (u && !isNaN(u))
      ? ` · ${String(u.getHours()).padStart(2, '0')}:${String(u.getMinutes()).padStart(2, '0')}`
      : '';
    return `<span class="pill off">AV${until}</span>`;
  }
  return '<span class="pill ok">OK</span>';
}

// 4-bar signal meter: bar count from RSSI (−60/−80/−100 dBm thresholds),
// color from SNR (>= 5 ok, >= 0 warn, else bad; RSSI alone when SNR unknown).
// Classes only — the CSP (style-src 'self') blocks inline style attributes.
function sigMeter(d) {
  if (d.rssi == null) return '';
  const bars = d.rssi >= -60 ? 4 : d.rssi >= -80 ? 3 : d.rssi >= -100 ? 2 : 1;
  const cls = d.snr != null
    ? (d.snr >= 5 ? 'ok' : d.snr >= 0 ? 'warn' : 'bad')
    : (d.rssi >= -80 ? 'ok' : d.rssi >= -100 ? 'warn' : 'bad');
  let h = '';
  for (let i = 1; i <= 4; i++) h += `<i class="h${i}${i <= bars ? ' on' : ''}"></i>`;
  return `<span class="sig ${cls}" title="RSSI ${d.rssi}dBm · SNR ${d.snr ?? '—'}dB">${h}</span>`;
}

// flott-raden (v2): korta ålderstexter ("5 min"), röd efter > 24 h
function relAge(iso) {
  if (!iso) return { text: '—', bad: false };
  const t = new Date(iso).getTime();
  if (isNaN(t)) return { text: '—', bad: false };
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return { text: 'just nu', bad: false };
  const m = s / 60;
  if (m < 60) return { text: `${Math.floor(m)} min`, bad: false };
  const h = m / 60;
  if (h < 24) return { text: `${Math.floor(h)} h`, bad: false };
  return { text: `${Math.floor(h / 24)} d`, bad: true };
}

// Z4: batteri med ikon; bärnsten när battery_pct < 20, inget span utan data
function battStr(d) {
  const parts = [];
  if (d.battery_voltage != null) parts.push(`${d.battery_voltage.toFixed(2)}V`);
  if (d.battery_pct != null) parts.push(`${Math.round(d.battery_pct)}%`);
  if (!parts.length) return '';
  const low = d.battery_pct != null && d.battery_pct < 20;
  return `<span class="bat${low ? ' low' : ''}">${BATT_ICON}${esc(parts.join(' · '))}</span>`;
}

function renderFleet() {
  renderBanners('#banners');
  const sorted = [...DEVICES].sort((a, b) => {
    const hot = (d) => (d.active?.length || recentFor(d).length ? 0 : 1);
    const ra = hot(a) - hot(b);
    if (ra !== 0) return ra;
    const rb = (a.armed ? 1 : 0) - (b.armed ? 1 : 0);
    if (rb !== 0) return rb;
    return a.name.localeCompare(b.name);
  });
  $('#fleet').innerHTML = sorted.map((d) => {
    // Z0: allvarslist — röd vid larm, bärnsten vid batteri-varning
    const row = d.active?.length
      ? (d.active.every((k) => WARN_KINDS.has(k)) ? ' warn-r' : ' alarm-r')
      : '';
    const grp = [d.group, d.following ? '' : 'följs ej'].filter(Boolean).join(' · ');
    const age = relAge(d.last_seen);
    return `
    <div class="card dev clickable${row}${d.armed ? '' : ' dimmed'}" data-eui="${esc(d.dev_eui)}">
      <div class="ic">${KIND_ICON[d.kind] || KIND_ICON.sensor}</div>
      <div class="c1">
        <span class="nm">${esc(d.name)}</span>
        <small class="grp">${esc(grp)}</small>
        ${pillFor(d)}
      </div>
      <div class="c2">
        ${d.temperature != null ? `<span class="temp">${d.temperature.toFixed(1)}°C</span>` : '<span class="nn">—</span>'}
        ${battStr(d)}
        <span class="sp"></span>
        <span class="seen${age.bad ? ' bad' : ''}">${esc(age.text)}</span>
        ${sigMeter(d)}
      </div>
    </div>`;
  }).join('');
  $('#fleet').querySelectorAll('.card').forEach((c) =>
    c.addEventListener('click', () => { location.hash = '#/dev/' + c.dataset.eui; }));
}

function renderDevice(eui) {
  const d = device(eui);
  if (!d) { location.hash = '#/'; return; }
  renderBanners('#dev-banners');
  const rows = [
    ['Temperatur', d.temperature != null ? `${d.temperature.toFixed(1)}°C` : '—'],
    ['Batteri', d.battery_voltage != null
      ? `${d.battery_voltage.toFixed(2)}V${d.battery_pct != null ? `·${Math.round(d.battery_pct)}%` : ''}` : '—'],
    ['Senaste uplink', relTime(d.last_seen)],
    ['RSSI / SNR', d.rssi != null ? `${sigMeter(d)} ${d.rssi}dBm/${d.snr ?? '—'}dB` : '—', true],
    ['devEUI', d.dev_eui],
  ];
  $('#dev-kv').innerHTML = rows.map(([k, v, raw]) =>
    `<div class="rowk"><span class="k">${esc(k)}</span><span class="v">${raw ? v : esc(v)}</span></div>`).join('');
  const tg = $('#follow-toggle');
  tg.classList.toggle('on', !!d.following);
  tg.onclick = () => toggleFollow(d);
  renderArm(d);
}

function renderArm(d) {
  const sw = $('#arm-switch');
  sw.classList.toggle('on', !!d.armed);
  $('#arm-lbl').textContent = d.armed ? '🛡 Larm: PÅ' : '🛡 Larm: AV';
  $('#arm-sub').textContent = d.armed
    ? 'Slå av med snooze (slår på sig själv) — eller tills vidare för admin.'
    : `Återaktiveras ${fmtUntil(d.armed_until) || 'automatiskt vid snooze-slut'}.`;
  // the picker stays open across the 15 s refresh; it closes once the device is
  // disarmed or the user navigates away
  const open = d.armed && SNOOZE_OPEN === d.dev_eui;
  $('#snooze-row').classList.toggle('hidden', !open);
  $('#snooze-forever').classList.toggle('hidden', ME?.role !== 'admin');
  sw.onclick = async () => {
    try {
      if (d.armed) {
        SNOOZE_OPEN = SNOOZE_OPEN === d.dev_eui ? null : d.dev_eui;
        renderArm(d);
      } else {
        await api(`/api/devices/${d.dev_eui}/armed`, { method: 'PUT', body: { armed: true } });
        toast('Larm på');
        await refresh();
      }
    } catch (e) { toast(e.message); }
  };
  $('#snooze-row').querySelectorAll('.snooze').forEach((b) => {
    b.onclick = async () => {
      try {
        await api(`/api/devices/${d.dev_eui}/armed`,
          { method: 'PUT', body: { armed: false, snooze_hours: Number(b.dataset.h) } });
        SNOOZE_OPEN = null;
        toast(`Larm av i ${b.dataset.h} h`);
        await refresh();
      } catch (e) { toast(e.message); }
    };
  });
  $('#snooze-forever').onclick = async () => {
    try {
      await api(`/api/devices/${d.dev_eui}/armed`, { method: 'PUT', body: { armed: false } });
      SNOOZE_OPEN = null;
      toast('Larm av tills vidare');
      await refresh();
    } catch (e) { toast(e.message); }
  };
}

async function toggleFollow(d) {
  try {
    const subs = await api('/api/subscriptions');
    const set = new Set(subs.devices);
    if (set.has(d.dev_eui)) set.delete(d.dev_eui); else set.add(d.dev_eui);
    await api('/api/subscriptions', { method: 'PUT', body: { devices: [...set] } });
    await refresh();
    route();
  } catch (e) { toast(e.message); }
}

// ---------- settings / push ----------

async function pushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.getRegistration('/');
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

async function renderSettings() {
  $('#me-name').textContent = ME ? `Inloggad som ${ME.name}${ME.role === 'admin' ? ' (admin)' : ''}` : '';
  $('#ios-hint').classList.toggle('hidden', !(IS_IOS && !runningStandalone()));
  renderInstall();

  const themeTg = $('#theme-toggle');
  themeTg.classList.toggle('on', localStorage.getItem(THEME_KEY) === 'home');
  themeTg.onclick = toggleTheme;

  const sub = await pushSubscription();
  const state = $('#push-state'), meta = $('#push-meta');
  if (!('PushManager' in window)) {
    state.textContent = 'Push stöds inte i denna webbläsare';
    meta.textContent = '';
  } else if (sub) {
    state.textContent = 'Push aktiverad på denna enhet';
    meta.textContent = 'Du får larm som notiser även när appen är stängd';
    $('#push-test').classList.remove('hidden');
  } else {
    state.textContent = 'Push ej aktiverad';
    meta.textContent = 'Aktivera för att få larm som notiser';
    $('#push-test').classList.add('hidden');
  }
  $('#push-enable').classList.toggle('hidden', !!sub || !('PushManager' in window));

  const list = $('#mute-list');
  list.innerHTML = DEVICES.map((d) => `
    <div class="card">
      <div class="ic">${KIND_ICON[d.kind] || KIND_ICON.sensor}</div>
      <div><div class="nm">${esc(d.name)}</div>
        <div class="meta">${d.armed ? '🛡 larm på' : '🛡 larm av ' + esc(fmtUntil(d.armed_until))}</div></div>
      <span class="tg ${d.following ? 'on' : ''}" data-eui="${esc(d.dev_eui)}"></span>
    </div>`).join('');
  list.querySelectorAll('.tg').forEach((tg) => {
    tg.addEventListener('click', () => toggleFollow(device(tg.dataset.eui)));
  });
}

function b64urlToBuffer(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const bin = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function sameKey(buf, bytes) {
  if (!buf) return false;
  const a = new Uint8Array(buf);
  return a.length === bytes.length && a.every((v, i) => v === bytes[i]);
}

async function enablePush() {
  try {
    if (!('Notification' in window) || !('serviceWorker' in navigator)) {
      toast('Push stöds inte i denna webbläsare');
      return;
    }
    // first thing in the click handler: Safari drops the user gesture after an await
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { toast('Notisbehörighet nekades'); return; }
    await navigator.serviceWorker.register('/sw.js');
    const reg = await navigator.serviceWorker.ready;   // subscribe needs an *active* worker
    const { public_key } = await api('/api/push/key');
    const key = b64urlToBuffer(public_key);
    let sub = await reg.pushManager.getSubscription();
    if (sub && !sameKey(sub.options && sub.options.applicationServerKey, key)) {
      await sub.unsubscribe();           // made with an old VAPID key: unusable
      sub = null;
    }
    if (!sub) {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    }
    await api('/api/push/subscribe', {
      method: 'POST',
      body: { endpoint: sub.endpoint, keys: sub.toJSON().keys },
    });
    toast('Push aktiverad');
    renderSettings();
  } catch (e) { toast(e.message); }
}

async function sendTestPush() {
  try {
    const r = await api('/api/push/test', { method: 'POST', body: {} });
    toast(r.endpoints > 0 ? `Test-push skickad (${r.endpoints} enhet${r.endpoints > 1 ? 'er' : ''})`
                          : 'Ingen push-enhet registrerad än');
  } catch (e) { toast(e.message); }
}

async function logout() {
  // independent steps: a failed push cleanup must never leave the session alive
  try {
    const sub = await pushSubscription();
    if (sub) {
      await api('/api/push/subscribe', { method: 'DELETE', body: { endpoint: sub.endpoint } });
      await sub.unsubscribe().catch(() => {});
    }
  } catch (_) { /* best effort */ }
  try {
    await api('/api/logout', { method: 'POST', body: {} });
  } catch (e) {
    if (e.status !== 401) { toast('Utloggningen misslyckades: ' + e.message); return; }
  }
  ME = null;
  DEVICES = [];
  STATE = { ok: true, alarms: [], recent: [] };
  location.hash = '#/login';
  showLogin();
}

// ---------- data ----------

async function refresh() {
  const [dev, st] = await Promise.all([api('/api/devices'), api('/api/state')]);
  DEVICES = dev.devices;
  WEBPUSH_ON = dev.webpush;
  STATE = st;
  LAST_OK = Date.now();
  FAILS = 0;
}

async function tick() {
  if (!ME || TICKING) return;
  TICKING = true;
  try {
    await refresh();
    if (!$('#view-fleet').classList.contains('hidden')) renderFleet();
    renderChrome();
    const m = location.hash.match(/^#\/dev\/([0-9a-f]{16})$/);
    if (m) renderDevice(m[1]);
    if (location.hash === '#/settings') renderSettings();
  } catch (e) {
    if (e.status !== 401) FAILS++;     // 401 -> login handled in api()
    renderFleet();                     // drops the green banner when stale
  } finally {
    TICKING = false;
    renderConn();
  }
}

// ---------- install (PWA) ----------

// the same instructions (#install-tpl) are shown on the login view and in settings
const INSTALL_UI = [
  { btn: '#install-btn', help: '#install-help' },
  { btn: '#install-btn-settings', help: '#install-help-settings' },
];

function setInstallHelp({ btn, help }, open) {
  $(help).classList.toggle('hidden', !open);
  $(btn).setAttribute('aria-expanded', String(open));
}

function setupInstall() {
  const tpl = $('#install-tpl');
  for (const ui of INSTALL_UI) {
    const box = $(ui.help);
    box.append(tpl.content.cloneNode(true));
    box.querySelectorAll('[data-platform]').forEach((el) => {
      const p = el.dataset.platform;
      el.classList.toggle('hidden', (IS_IOS && p !== 'ios') || (IS_ANDROID && p !== 'android'));
    });
    box.querySelector('.install-native').addEventListener('click', nativeInstall);
    $(ui.btn).addEventListener('click', () => setInstallHelp(ui, box.classList.contains('hidden')));
  }
  if (IS_IOS) setInstallHelp(INSTALL_UI[1], true);   // iOS needs the install before push works
}

// state only: leaves open/closed panels as the user left them
function renderInstall() {
  const installed = runningStandalone();
  for (const ui of INSTALL_UI) {
    $(ui.btn).classList.toggle('hidden', installed);
    if (installed) setInstallHelp(ui, false);
    $(ui.help).querySelectorAll('.install-native')
      .forEach((b) => b.classList.toggle('hidden', !DEFERRED_INSTALL));
  }
}

async function nativeInstall() {
  if (!DEFERRED_INSTALL) return;
  const prompt = DEFERRED_INSTALL;
  prompt.prompt();
  const { outcome } = await prompt.userChoice;
  DEFERRED_INSTALL = null;          // the browser fires it only once
  renderInstall();
  toast(outcome === 'accepted'
    ? 'Installerar appen…'
    : 'Installationen avbröts – du kan även använda webbläsarens meny');
}

// ---------- boot ----------

async function boot() {
  applyTheme();                     // sätt body.theme-home innan första målningen
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  $('#login-btn').addEventListener('click', doLogin);
  $('#login-token').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('#tab-fleet').addEventListener('click', () => { location.hash = '#/'; });
  $('#tab-settings').addEventListener('click', () => { location.hash = '#/settings'; });
  $('#dev-back').addEventListener('click', () => { location.hash = '#/'; });
  $('#push-enable').addEventListener('click', enablePush);
  $('#push-test').addEventListener('click', sendTestPush);
  $('#logout-btn').addEventListener('click', logout);
  setupInstall();
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();        // we show our own "Installera nu" button instead of the mini Infobar
    DEFERRED_INSTALL = e;
    renderInstall();
  });
  window.addEventListener('appinstalled', () => {
    DEFERRED_INSTALL = null;
    renderInstall();           // installed: drop buttons and panels
  });
  window.addEventListener('hashchange', route);
  // phones suspend timers: refresh as soon as the app is visible / back online
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  window.addEventListener('online', tick);
  if ('serviceWorker' in navigator) {
    // notification tapped while the app is already open
    navigator.serviceWorker.addEventListener('message', (e) => {
      const dev = e.data && e.data.type === 'open' && e.data.dev;
      if (ME && EUI_RE.test(dev || '')) location.hash = '#/dev/' + dev;
      tick();
    });
  }

  // started before the session check: after a fresh login (doLogin) polling
  // must run too; tick() is a no-op while ME is unset
  setInterval(tick, POLL_MS);
  setInterval(renderConn, 5000);      // also while polls are failing or frozen

  try {
    ME = await api('/api/me');        // session cookie still valid?
    await refresh();
  } catch (_) {
    showLogin();                     // api() already switched on 401; also network errors
    return;
  }
  showApp();
  try {
    renderFleet();
    if (new URLSearchParams(location.search).has('dev')) location.hash = pendingTarget();
    route();
  } catch (e) {
    // a rendering bug must never look like "logged out" (that masked the
    // swipe-item-recent class crash as a phantom logout)
    toast('Kunde inte rita upp statusvyn: ' + e.message);
  }
}

// a push notification opens /?dev=<eui> — kept across login and for an open session
function pendingTarget() {
  const dev = new URLSearchParams(location.search).get('dev') || '';
  return EUI_RE.test(dev) ? `#/dev/${dev}` : '#/';
}

async function doLogin() {
  const token = $('#login-token').value.trim();
  $('#login-err').textContent = '';
  if (!token) return;
  try {
    const r = await api('/api/login', { method: 'POST', body: { token } });
    ME = { name: r.name, role: r.role };
    $('#login-token').value = '';
    await refresh();
    showApp();
    location.hash = pendingTarget();
    renderFleet();
    route();
    toast(`Inloggad som ${r.name}`);
  } catch (e) {
    $('#login-err').textContent = e.status === 401 ? 'Fel inbjudningstoken' : e.message;
  }
}

boot();
