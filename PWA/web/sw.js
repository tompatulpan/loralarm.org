/* LoRaLarm service worker.
 * push: always showNotification (iOS revokes subscriptions that show nothing),
 *   tag = dev_eui:kind so a clear replaces the fire notification, then report
 *   delivery via POST /api/push/ack (X-Requested-With, like every mutation).
 * notificationclick: focus an open app window, else open the app (at the device
 *   when the notification carries one). */
'use strict';

self.addEventListener('install', () => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { /* non-JSON push */ }
  const tag = `${d.dev_eui || 'loralarm'}:${d.kind || 'info'}`;
  e.waitUntil(
    self.registration.showNotification(d.title || 'LoRaLarm', {
      body: d.body || '',
      tag,
      renotify: true,
      data: { dev_eui: d.dev_eui || '', event_id: d.event_id || null },
    }).then(() => ackDelivery(d))
  );
});

async function ackDelivery(d) {
  if (!d.event_id) return;   // test pushes have no event to ack against
  try {
    const sub = await self.registration.pushManager.getSubscription();
    if (!sub) return;
    await fetch('/api/push/ack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'loralarm' },
      body: JSON.stringify({ event_id: d.event_id, endpoint: sub.endpoint }),
    });
  } catch (_) { /* ack is best-effort */ }
}

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const dev = e.notification.data && e.notification.data.dev_eui;
  const target = dev ? `/?dev=${encodeURIComponent(dev)}` : '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) return w.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
