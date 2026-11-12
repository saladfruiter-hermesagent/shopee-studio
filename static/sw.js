'use strict';
// Service worker mínimo. Ele existe por dois motivos: o Chrome no Android só oferece "Instalar
// aplicativo" para uma página com manifest + service worker com handler de fetch, e os estáticos
// versionados (?v=<APP_VERSION>) podem vir do cache.
//
// O que NUNCA é cacheado: HTML, /api/* e o próprio login. O painel mostra dado de produção e sessão
// autenticada — servir uma resposta antiga de /api/state daria tela mentirosa (fila de vídeos, teto de
// créditos, entregas). Por isso o handler só assume GET de /static/ com ?v=.

const CACHE = 'shopee-studio-static-v1';

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  const versionado = url.origin === self.location.origin
    && url.pathname.startsWith('/static/')
    && url.searchParams.has('v');
  if (!versionado) return;  // rede, sem interceptar: HTML, API, login

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  })());
});
