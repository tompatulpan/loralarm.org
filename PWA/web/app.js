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
};
const KIND_ICON = { door: '🚪', water: '💧', button: '🔘', sensor: '🌡️' };

let ME = null;            // {name, role}
let DEVICES = [];         // GET /api/devices
let STATE = { ok: true, alarms: [] };   // GET /api/state
let WEBPUSH_ON = false;
let LAST_OK = 0;          // ms timestamp of the last successful refresh
let FAILS = 0;            // consecutive failed refreshes
let SNOOZE_OPEN = null;   // devEUI whose snooze picker is open (survives re-renders)
let TICKING = false;
const POLL_MS = 15000;
const STALE_AFTER_MS = 2.5 * POLL_MS;   // ~2 missed polls
const EUI_RE = /^[0-9a-f]{16}$/;

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
  const badge = $('#alarm-badge');
  badge.classList.toggle('hidden', n === 0);
  badge.textContent = n > 0 ? `${n} larm aktiv${n > 1 ? 'a' : ''}` : '';
}

function renderBanners(el) {
  const host = $(el);
  if (!STATE.alarms.length) {
    host.innerHTML = isStale() ? '' : `<div class="banner ok"><div class="dot"></div>
      <div><div class="t">Inga aktiva larm</div><div class="d">Alla enheter är tysta</div></div></div>`;
    return;
  }
  host.innerHTML = STATE.alarms.map((a) => `
    <div class="banner">
      <div class="dot"></div>
      <div>
        <div class="t">${esc(KIND_LABEL[a.kind] || a.kind)}: ${esc(a.name)}</div>
        <div class="d">${esc(a.detail)}</div>
      </div>
    </div>`).join('');
}

function pillFor(d) {
  if (d.active && d.active.length) {
    return `<span class="pill alarm">${d.active.map((k) => esc(KIND_LABEL[k] || k)).join(', ').toUpperCase()}</span>`;
  }
  if (!d.armed) return '<span class="pill off">LARM AV</span>';
  return '<span class="pill ok">OK</span>';
}

function battLine(d) {
  const parts = [];
  if (d.battery_voltage != null) parts.push(`${d.battery_voltage.toFixed(2)} V`);
  if (d.battery_pct != null) parts.push(`${Math.round(d.battery_pct)} %`);
  parts.push(relTime(d.last_seen));
  const low = d.battery_pct != null && d.battery_pct < 20;
  const b = parts.slice(0, 2).join(' · ');
  return `<div class="batt">${b ? `<b class="${low ? 'low' : ''}">${esc(b)}</b> · ` : ''}${esc(parts[parts.length - 1])}</div>`;
}

function renderFleet() {
  renderBanners('#banners');
  const sorted = [...DEVICES].sort((a, b) => {
    const ra = (a.active?.length ? 0 : 1) - (b.active?.length ? 0 : 1);
    if (ra !== 0) return ra;
    const rb = (a.armed ? 1 : 0) - (b.armed ? 1 : 0);
    if (rb !== 0) return rb;
    return a.name.localeCompare(b.name);
  });
  $('#fleet').innerHTML = sorted.map((d) => `
    <div class="card clickable${d.armed ? '' : ' dimmed'}" data-eui="${esc(d.dev_eui)}">
      <div class="ic">${KIND_ICON[d.kind] || KIND_ICON.sensor}</div>
      <div>
        <div class="nm">${esc(d.name)}
          <span class="arm ${d.armed ? 'on' : 'off'}">🛡 larm ${d.armed ? 'på' : 'av'}${d.armed ? '' : ' ' + esc(fmtUntil(d.armed_until))}</span>
        </div>
        <div class="meta">${esc(d.family)}${d.group ? ' · ' + esc(d.group) : ''}${d.following ? '' : ' · följs ej'}</div>
      </div>
      <div class="right">${pillFor(d)}${battLine(d)}</div>
    </div>`).join('');
  $('#fleet').querySelectorAll('.card').forEach((c) =>
    c.addEventListener('click', () => { location.hash = '#/dev/' + c.dataset.eui; }));
}

function renderDevice(eui) {
  const d = device(eui);
  if (!d) { location.hash = '#/'; return; }
  renderBanners('#dev-banners');
  const rows = [
    ['Temperatur', d.temperature != null ? `${d.temperature.toFixed(1)} °C` : '—'],
    ['Batteri', d.battery_voltage != null
      ? `${d.battery_voltage.toFixed(2)} V${d.battery_pct != null ? ` · ${Math.round(d.battery_pct)} %` : ''}` : '—'],
    ['Senaste uplink', relTime(d.last_seen)],
    ['RSSI / SNR', d.rssi != null ? `${d.rssi} dBm / ${d.snr ?? '—'}` : '—'],
    ['devEUI', d.dev_eui],
  ];
  $('#dev-kv').innerHTML = rows.map(([k, v]) =>
    `<div class="rowk"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('');
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
  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
  const standalone = window.matchMedia('(display-mode: standalone)').matches
    || navigator.standalone === true;
  $('#ios-hint').classList.toggle('hidden', !(isIOS && !standalone));

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
  STATE = { ok: true, alarms: [] };
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

// ---------- boot ----------

async function boot() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  $('#login-btn').addEventListener('click', doLogin);
  $('#login-token').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('#tab-fleet').addEventListener('click', () => { location.hash = '#/'; });
  $('#tab-settings').addEventListener('click', () => { location.hash = '#/settings'; });
  $('#dev-back').addEventListener('click', () => { location.hash = '#/'; });
  $('#push-enable').addEventListener('click', enablePush);
  $('#push-test').addEventListener('click', sendTestPush);
  $('#logout-btn').addEventListener('click', logout);
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

  try {
    ME = await api('/api/me');        // session cookie still valid?
    await refresh();
    showApp();
    renderFleet();
    if (new URLSearchParams(location.search).has('dev')) location.hash = pendingTarget();
    route();
  } catch (_) {
    showLogin();
  }
  setInterval(tick, POLL_MS);
  setInterval(renderConn, 5000);      // also while polls are failing or frozen
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
