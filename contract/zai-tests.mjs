#!/usr/bin/env node
// ZAI 26Z – testy automatyczne projektu „Serie pomiarowe”
// Wymaga Node.js 20+ (wbudowany fetch). Brak zewnętrznych zależności.
//
// Tryb studenta (jedna aplikacja):
//   node zai-tests.mjs --app https://moja-app.example.com --user admin --pass 'haslo' --stage E2
//   (opcjonalnie --api https://api.moja-app.example.com, jeśli API działa pod innym adresem)
//
// Tryb prowadzącego (wiele zgłoszeń z pliku CSV):
//   node zai-tests.mjs --csv zgloszenia.csv --stage E2 --out wyniki.csv [--lighthouse]
//   Kolumny CSV: student,app_url,api_url,username,password  (api_url może być puste)

import { parseArgs } from 'node:util';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

// ---------------------------------------------------------------------------
// Punktacja (zgodna z sekcją 5 specyfikacji)
// ---------------------------------------------------------------------------
const SCORING = {
  E1: { main: { kontrakt: 10 }, bonus: {} },
  E2: {
    main: { kontrakt: 6, walidacja: 4, bezpieczenstwo: 3, lighthouse: 3 },
    bonus: {}, // punkty dodatkowe przyznaje prowadzący na podstawie nagrania i kodu
  },
};
const CODES = { kontrakt: 'A1/A2', walidacja: 'A3', bezpieczenstwo: 'A4', lighthouse: 'A5' };

const TIMEOUT_MS = 20_000;
const WARMUP_TRIES = 6; // ok. 2 minuty na „wybudzenie” darmowego hostingu

// ---------------------------------------------------------------------------
// Narzędzia: HTTP, asercje, rejestr testów
// ---------------------------------------------------------------------------
class TestFailure extends Error {}
function expect(cond, msg) { if (!cond) throw new TestFailure(msg); }
function expectStatus(res, allowed, what) {
  const list = Array.isArray(allowed) ? allowed : [allowed];
  expect(list.includes(res.status), `${what}: oczekiwano ${list.join(' lub ')}, otrzymano ${res.status}`);
}

function joinUrl(base, p) { return base.replace(/\/+$/, '') + p; }

async function http(ctx, method, p, { body, token, headers = {}, rawBody, redirect = 'follow', base } = {}) {
  const h = { Accept: 'application/json', ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  let payload;
  if (rawBody !== undefined) payload = rawBody;
  else if (body !== undefined) { payload = JSON.stringify(body); h['Content-Type'] ??= 'application/json'; }
  const url = /^https?:/.test(p) ? p : joinUrl(base ?? ctx.api, p);
  const res = await fetch(url, { method, headers: h, body: payload, redirect, signal: AbortSignal.timeout(TIMEOUT_MS) });
  const text = await res.text();
  let json;
  try { json = text ? JSON.parse(text) : undefined; } catch { json = undefined; }
  return { status: res.status, headers: res.headers, text, json };
}

const isId = (v) => (typeof v === 'number' && Number.isInteger(v)) || (typeof v === 'string' && v.length > 0);
const sameInstant = (a, b) => Date.parse(a) === Date.parse(b);
const contentType = (res) => (res.headers.get('content-type') || '').toLowerCase();

function checkSeries(s, what = 'Seria') {
  expect(s && typeof s === 'object', `${what}: odpowiedź nie jest obiektem JSON`);
  expect(isId(s.id), `${what}: brak poprawnego pola id`);
  expect(typeof s.name === 'string', `${what}: brak pola name`);
  expect(typeof s.minValue === 'number' && typeof s.maxValue === 'number', `${what}: minValue/maxValue muszą być liczbami`);
  expect(/^#[0-9a-f]{6}$/i.test(s.color ?? ''), `${what}: pole color musi mieć format #RRGGBB`);
}
function checkMeasurement(m, what = 'Wynik') {
  expect(m && typeof m === 'object', `${what}: odpowiedź nie jest obiektem JSON`);
  expect(isId(m.id), `${what}: brak poprawnego pola id`);
  expect(isId(m.seriesId), `${what}: brak pola seriesId`);
  expect(typeof m.value === 'number', `${what}: value musi być liczbą`);
  expect(!Number.isNaN(Date.parse(m.timestamp)), `${what}: timestamp nie jest poprawną datą ISO 8601`);
}
function checkProblem(res, what) {
  const ct = contentType(res);
  expect(ct.includes('application/problem+json') || ct.includes('application/json'), `${what}: błąd powinien mieć typ application/problem+json`);
  expect(res.json && typeof res.json.status === 'number' && typeof res.json.title === 'string',
    `${what}: treść błędu powinna zawierać pola title i status (RFC 9457)`);
}
function locationPath(res, what) {
  const loc = res.headers.get('location');
  expect(loc, `${what}: brak nagłówka Location`);
  return loc;
}
function sortedAsc(list) {
  for (let i = 1; i < list.length; i++) if (Date.parse(list[i - 1].timestamp) > Date.parse(list[i].timestamp)) return false;
  return true;
}

const TESTS = [];
function test(group, name, fn) { TESTS.push({ group, name, fn }); }

// ---------------------------------------------------------------------------
// Dane testowe i funkcje pomocnicze
// ---------------------------------------------------------------------------
const TEST_PREFIX = 'ZAI-TEST-';
const tag = () => `${TEST_PREFIX}${Math.random().toString(36).slice(2, 8)}`;
const seriesInput = (over = {}) => ({ name: tag(), minValue: 0, maxValue: 100, color: '#1f77b4', ...over });
const T0 = '2026-01-10T10:00:00Z';
const T1 = '2026-01-10T11:00:00Z';
const T2 = '2026-01-10T12:00:00Z';
const NOWISH_MS = 10 * 60_000;

async function adminToken(ctx) {
  if (ctx.token) return ctx.token;
  const res = await http(ctx, 'POST', '/api/auth/login', { body: { username: ctx.user, password: ctx.pass } });
  expectStatus(res, 200, 'Logowanie danymi z formularza');
  expect(res.json?.accessToken, 'Logowanie: brak accessToken w odpowiedzi');
  ctx.token = res.json.accessToken;
  return ctx.token;
}
async function newSeries(ctx, over) {
  const res = await http(ctx, 'POST', '/api/series', { body: seriesInput(over), token: await adminToken(ctx) });
  expectStatus(res, 201, 'Utworzenie serii pomocniczej');
  ctx.cleanup.series.add(res.json.id);
  return res.json;
}
async function newSensor(ctx, seriesId) {
  const res = await http(ctx, 'POST', '/api/sensors', { body: { name: tag(), seriesId }, token: await adminToken(ctx) });
  expectStatus(res, 201, 'Rejestracja czujnika pomocniczego');
  expect(typeof res.json?.apiKey === 'string', 'Rejestracja czujnika: brak apiKey w odpowiedzi');
  ctx.cleanup.sensors.add(res.json.id);
  return res.json;
}
// Seria z przypisanym czujnikiem – podstawowy „zestaw” większości testów
async function seriesWithSensor(ctx, over) {
  const series = await newSeries(ctx, over);
  const sensor = await newSensor(ctx, series.id);
  return { series, sensor, key: sensor.apiKey };
}
const send = (ctx, key, body, extra = {}) =>
  http(ctx, 'POST', '/api/measurements', { body, headers: key ? { 'X-API-Key': key } : {}, ...extra });
async function sendOk(ctx, key, body) {
  const res = await send(ctx, key, body);
  expectStatus(res, 201, 'Przesłanie wyniku pomocniczego kluczem czujnika');
  return res.json;
}

// ---------------------------------------------------------------------------
// Grupa: kontrakt (A1 w E1, A2 w E2)
// ---------------------------------------------------------------------------
test('kontrakt', 'GET /api/health zwraca 200 i {"status":"ok"}', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/health');
  expectStatus(res, 200, 'GET /api/health');
  expect(res.json?.status === 'ok', 'Oczekiwano {"status":"ok"}');
});

test('kontrakt', 'GET /api/series zwraca tablicę JSON (publicznie)', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/series');
  expectStatus(res, 200, 'GET /api/series');
  expect(contentType(res).includes('application/json'), 'Content-Type powinien być application/json');
  expect(Array.isArray(res.json), 'Odpowiedź powinna być tablicą');
  res.json.forEach((s, i) => checkSeries(s, `Seria [${i}]`));
});

test('kontrakt', 'Dane przykładowe: co najmniej 3 serie po co najmniej 15 wyników (F11)', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/series');
  expectStatus(res, 200, 'GET /api/series');
  const own = (res.json ?? []).filter((s) => !String(s.name).startsWith(TEST_PREFIX));
  let full = 0;
  for (const s of own) {
    const m = await http(ctx, 'GET', `/api/measurements?series=${s.id}&limit=10000`);
    if (m.status === 200 && Array.isArray(m.json) && m.json.length >= 15) full++;
    if (full >= 3) break;
  }
  expect(full >= 3, `Znaleziono ${full} serii z co najmniej 15 wynikami (wymagane 3)`);
});

test('kontrakt', 'POST /api/auth/login zwraca token Bearer', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/auth/login', { body: { username: ctx.user, password: ctx.pass } });
  expectStatus(res, 200, 'Logowanie');
  expect(typeof res.json?.accessToken === 'string' && res.json.accessToken.length > 0, 'Brak accessToken');
  expect(res.json?.tokenType === 'Bearer', 'tokenType powinien mieć wartość "Bearer"');
  expect(Number.isInteger(res.json?.expiresIn), 'expiresIn powinien być liczbą całkowitą');
  ctx.token = res.json.accessToken;
});

test('kontrakt', 'POST /api/series tworzy serię (201 + Location)', async (ctx) => {
  const input = seriesInput({ unit: '°C' });
  const res = await http(ctx, 'POST', '/api/series', { body: input, token: await adminToken(ctx) });
  expectStatus(res, 201, 'POST /api/series');
  checkSeries(res.json);
  ctx.cleanup.series.add(res.json.id);
  expect(res.json.name === input.name && res.json.minValue === 0 && res.json.maxValue === 100, 'Zwrócona seria nie odpowiada wysłanym danym');
  ctx.s = { ...res.json, location: locationPath(res, 'POST /api/series') };
});

test('kontrakt', 'GET na adres z Location zwraca utworzoną serię', async (ctx) => {
  const res = await http(ctx, 'GET', ctx.s.location);
  expectStatus(res, 200, `GET ${ctx.s.location}`);
  checkSeries(res.json);
  expect(String(res.json.id) === String(ctx.s.id), 'Identyfikator serii się nie zgadza');
});

test('kontrakt', 'PUT /api/series/{id} aktualizuje serię (200)', async (ctx) => {
  const upd = seriesInput({ name: ctx.s.name + '-upd', color: '#ff7f0e' });
  const res = await http(ctx, 'PUT', `/api/series/${ctx.s.id}`, { body: upd, token: await adminToken(ctx) });
  expectStatus(res, 200, 'PUT /api/series/{id}');
  checkSeries(res.json);
  expect(res.json.name === upd.name && res.json.color.toLowerCase() === '#ff7f0e', 'Zmiany nie zostały zapisane');
});

test('kontrakt', 'POST /api/sensors rejestruje czujnik i zwraca klucz API (201 + Location)', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/sensors', { body: { name: tag(), seriesId: ctx.s.id }, token: await adminToken(ctx) });
  expectStatus(res, 201, 'POST /api/sensors');
  expect(isId(res.json?.id), 'Czujnik: brak poprawnego pola id');
  expect(String(res.json.seriesId) === String(ctx.s.id), 'Czujnik: seriesId nie wskazuje właściwej serii');
  expect(typeof res.json.apiKey === 'string' && res.json.apiKey.length >= 32, 'apiKey powinien mieć co najmniej 32 znaki');
  ctx.cleanup.sensors.add(res.json.id);
  ctx.sensor = { ...res.json, location: locationPath(res, 'POST /api/sensors') };
});

test('kontrakt', 'GET /api/sensors i /api/sensors/{id} zwracają czujnik bez klucza API', async (ctx) => {
  const t = await adminToken(ctx);
  const list = await http(ctx, 'GET', '/api/sensors', { token: t });
  expectStatus(list, 200, 'GET /api/sensors');
  expect(Array.isArray(list.json) && list.json.some((x) => String(x.id) === String(ctx.sensor.id)), 'Zarejestrowany czujnik nie występuje na liście');
  const one = await http(ctx, 'GET', `/api/sensors/${ctx.sensor.id}`, { token: t });
  expectStatus(one, 200, 'GET /api/sensors/{id}');
  for (const r of [list, one]) expect(!r.text.includes(ctx.sensor.apiKey), 'Odpowiedź zawiera klucz API czujnika');
});

test('kontrakt', 'POST /api/measurements kluczem czujnika zapisuje wynik (201 + Location)', async (ctx) => {
  const res = await send(ctx, ctx.sensor.apiKey, { value: 21.5, timestamp: T1 });
  expectStatus(res, 201, 'POST /api/measurements');
  checkMeasurement(res.json);
  expect(res.json.value === 21.5 && sameInstant(res.json.timestamp, T1), 'Zapisany wynik nie odpowiada wysłanym danym');
  expect(String(res.json.seriesId) === String(ctx.s.id), 'seriesId nie odpowiada serii przypisanej do czujnika');
  ctx.m = { ...res.json, location: locationPath(res, 'POST /api/measurements') };
});

test('kontrakt', 'Wynik bez timestamp otrzymuje bieżący czas serwera', async (ctx) => {
  const m = await sendOk(ctx, ctx.sensor.apiKey, { value: 5 });
  checkMeasurement(m);
  expect(Math.abs(Date.parse(m.timestamp) - Date.now()) < NOWISH_MS, 'timestamp powinien być zbliżony do bieżącego czasu');
});

test('kontrakt', 'GET na adres z Location zwraca zapisany wynik', async (ctx) => {
  const res = await http(ctx, 'GET', ctx.m.location);
  expectStatus(res, 200, `GET ${ctx.m.location}`);
  checkMeasurement(res.json);
  expect(res.json.value === 21.5, 'Odczytany wynik ma inną wartość niż zapisany');
});

test('kontrakt', 'GET /api/measurements?series= zwraca wyniki posortowane rosnąco', async (ctx) => {
  await sendOk(ctx, ctx.sensor.apiKey, { value: 10, timestamp: T2 });
  await sendOk(ctx, ctx.sensor.apiKey, { value: 30, timestamp: T0 });
  const res = await http(ctx, 'GET', `/api/measurements?series=${ctx.s.id}`);
  expectStatus(res, 200, 'GET /api/measurements');
  expect(Array.isArray(res.json), 'Odpowiedź powinna być tablicą');
  res.json.forEach((m) => checkMeasurement(m));
  expect(res.json.length === 4, `Oczekiwano 4 wyników serii testowej, otrzymano ${res.json.length}`);
  expect(res.json.every((m) => String(m.seriesId) === String(ctx.s.id)), 'Filtr series zwrócił wyniki innych serii');
  expect(sortedAsc(res.json), 'Wyniki nie są posortowane rosnąco według timestamp');
});

test('kontrakt', 'Filtrowanie from/to jest domknięte', async (ctx) => {
  const q = `/api/measurements?series=${ctx.s.id}&from=${encodeURIComponent(T0)}&to=${encodeURIComponent(T1)}`;
  const res = await http(ctx, 'GET', q);
  expectStatus(res, 200, 'GET z from/to');
  expect(res.json?.length === 2, `Oczekiwano 2 wyników w przedziale [T0, T1], otrzymano ${res.json?.length}`);
});

test('kontrakt', 'Filtr series obsługuje kilka serii naraz', async (ctx) => {
  const other = await seriesWithSensor(ctx);
  await sendOk(ctx, other.key, { value: 1, timestamp: T0 });
  const res = await http(ctx, 'GET', `/api/measurements?series=${ctx.s.id},${other.series.id}`);
  expectStatus(res, 200, 'GET z dwiema seriami');
  expect(res.json?.length === 5, `Oczekiwano 5 wyników z dwóch serii, otrzymano ${res.json?.length}`);
});

test('kontrakt', 'sort=-timestamp zwraca wyniki malejąco', async (ctx) => {
  const res = await http(ctx, 'GET', `/api/measurements?series=${ctx.s.id}&sort=-timestamp`);
  expectStatus(res, 200, 'GET z sort=-timestamp');
  expect(Array.isArray(res.json) && res.json.length >= 2 && sortedAsc([...res.json].reverse()), 'Wyniki nie są posortowane malejąco');
});

test('kontrakt', 'PUT serii z przedziałem wykluczającym istniejące wyniki zwraca 409', async (ctx) => {
  const res = await http(ctx, 'PUT', `/api/series/${ctx.s.id}`, {
    body: seriesInput({ name: ctx.s.name, minValue: 50, maxValue: 60 }), token: await adminToken(ctx),
  });
  expectStatus(res, 409, 'PUT serii z konfliktem przedziału');
});

test('kontrakt', 'Nieistniejąca seria: 404 w formacie Problem Details', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/series/999999999');
  expectStatus(res, 404, 'GET nieistniejącej serii');
  checkProblem(res, '404');
});

test('kontrakt', 'Accept: application/xml zwraca 406', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/series', { headers: { Accept: 'application/xml' } });
  expectStatus(res, 406, 'GET z Accept: application/xml');
});

test('kontrakt', 'Treść text/plain zwraca 415', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/measurements', {
    rawBody: 'value=1', headers: { 'Content-Type': 'text/plain', 'X-API-Key': ctx.sensor.apiKey },
  });
  expectStatus(res, 415, 'POST /api/measurements z Content-Type: text/plain');
});

test('kontrakt', 'Wyrejestrowanie czujnika: 204, klucz przestaje działać, wyniki pozostają', async (ctx) => {
  const del = await http(ctx, 'DELETE', `/api/sensors/${ctx.sensor.id}`, { token: await adminToken(ctx) });
  expectStatus(del, 204, 'DELETE /api/sensors/{id}');
  ctx.cleanup.sensors.delete(ctx.sensor.id);
  expectStatus(await send(ctx, ctx.sensor.apiKey, { value: 1 }), 401, 'Wynik wysłany kluczem wyrejestrowanego czujnika');
  expectStatus(await http(ctx, 'GET', ctx.m.location), 200, 'Odczyt wyniku zebranego przez wyrejestrowany czujnik');
});

test('kontrakt', 'DELETE serii usuwa ją razem z wynikami i czujnikami', async (ctx) => {
  const { series, sensor, key } = await seriesWithSensor(ctx);
  const m = await sendOk(ctx, key, { value: 1, timestamp: T0 });
  const t = await adminToken(ctx);
  expectStatus(await http(ctx, 'DELETE', `/api/series/${series.id}`, { token: t }), 204, 'DELETE serii');
  ctx.cleanup.series.delete(series.id);
  ctx.cleanup.sensors.delete(sensor.id);
  expectStatus(await http(ctx, 'GET', `/api/series/${series.id}`), 404, 'GET usuniętej serii');
  expectStatus(await http(ctx, 'GET', `/api/measurements/${m.id}`), 404, 'GET wyniku usuniętej serii');
  expectStatus(await http(ctx, 'GET', `/api/sensors/${sensor.id}`, { token: t }), 404, 'GET czujnika usuniętej serii');
  expectStatus(await send(ctx, key, { value: 1 }), 401, 'Wynik wysłany kluczem czujnika usuniętej serii');
});

test('kontrakt', 'POST /api/auth/logout unieważnia token', async (ctx) => {
  const login = await http(ctx, 'POST', '/api/auth/login', { body: { username: ctx.user, password: ctx.pass } });
  expectStatus(login, 200, 'Logowanie');
  const t = login.json.accessToken;
  expectStatus(await http(ctx, 'POST', '/api/auth/logout', { token: t }), 204, 'Wylogowanie');
  const after = await http(ctx, 'POST', '/api/series', { body: seriesInput(), token: t });
  if (after.status === 201) ctx.cleanup.series.add(after.json?.id);
  expectStatus(after, 401, 'Użycie tokenu po wylogowaniu');
});

// ---------------------------------------------------------------------------
// Grupa: walidacja i autoryzacja (A3)
// ---------------------------------------------------------------------------
test('walidacja', 'Wartość spoza przedziału serii: 422 i brak zapisu', async (ctx) => {
  const { series, key } = await seriesWithSensor(ctx, { minValue: 0, maxValue: 10 });
  const res = await send(ctx, key, { value: 11, timestamp: T0 });
  expectStatus(res, 422, 'Wynik 11 dla przedziału [0, 10]');
  checkProblem(res, '422');
  const list = await http(ctx, 'GET', `/api/measurements?series=${series.id}`);
  expect(Array.isArray(list.json) && list.json.length === 0, 'Odrzucony wynik mimo to został zapisany');
});

test('walidacja', 'Wartości graniczne min i max są akceptowane', async (ctx) => {
  const { key } = await seriesWithSensor(ctx, { minValue: -5, maxValue: 5 });
  await sendOk(ctx, key, { value: -5, timestamp: T0 });
  await sendOk(ctx, key, { value: 5, timestamp: T1 });
});

test('walidacja', 'Znacznik czasu z przyszłości (+1 h): 422', async (ctx) => {
  const { key } = await seriesWithSensor(ctx);
  const future = new Date(Date.now() + 3_600_000).toISOString();
  expectStatus(await send(ctx, key, { value: 1, timestamp: future }), 422, 'Wynik z przyszłości');
});

test('walidacja', 'Seria z minValue ≥ maxValue: 422', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/series', { body: seriesInput({ minValue: 10, maxValue: 10 }), token: await adminToken(ctx) });
  if (res.status === 201) ctx.cleanup.series.add(res.json?.id);
  expectStatus(res, 422, 'Seria z min = max');
});

test('walidacja', 'Niepoprawne typy i brak pól: 400 lub 422', async (ctx) => {
  const { key } = await seriesWithSensor(ctx);
  for (const body of [{ value: 'abc' }, {}, { value: 1, timestamp: 'wczoraj' }]) {
    expectStatus(await send(ctx, key, body), [400, 422], `Wynik ${JSON.stringify(body)}`);
  }
  const noName = await http(ctx, 'POST', '/api/series', { body: { minValue: 0, maxValue: 1, color: '#000000' }, token: await adminToken(ctx) });
  if (noName.status === 201) ctx.cleanup.series.add(noName.json?.id);
  expectStatus(noName, [400, 422], 'Seria bez nazwy');
});

test('walidacja', 'Niepoprawny JSON: 400', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/series', { rawBody: '{"name":', headers: { 'Content-Type': 'application/json' }, token: await adminToken(ctx) });
  expectStatus(res, 400, 'Uszkodzony JSON');
});

test('walidacja', 'Niepoprawny parametr from: 400', async (ctx) => {
  expectStatus(await http(ctx, 'GET', '/api/measurements?from=nie-data'), 400, 'GET z from=nie-data');
});

test('walidacja', 'Rejestracja czujnika dla nieistniejącej serii: 422', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/sensors', { body: { name: tag(), seriesId: 999999999 }, token: await adminToken(ctx) });
  if (res.status === 201) ctx.cleanup.sensors.add(res.json?.id);
  // Kontrakt wymaga 422; 404 jest akceptowane jako interpretacja dopuszczalna
  expectStatus(res, [422, 404], 'POST /api/sensors z nieistniejącą serią');
});

test('walidacja', 'Operacje administracyjne bez tokenu: 401', async (ctx) => {
  const { series, sensor } = await seriesWithSensor(ctx);
  const checks = [
    ['POST', '/api/series', seriesInput()],
    ['PUT', `/api/series/${series.id}`, seriesInput()],
    ['DELETE', `/api/series/${series.id}`],
    ['GET', '/api/sensors'],
    ['GET', `/api/sensors/${sensor.id}`],
    ['POST', '/api/sensors', { name: tag(), seriesId: series.id }],
    ['DELETE', `/api/sensors/${sensor.id}`],
  ];
  for (const [method, p, body] of checks) {
    const res = await http(ctx, method, p, { body });
    if (res.status === 201 && p === '/api/series') ctx.cleanup.series.add(res.json?.id);
    expectStatus(res, 401, `${method} ${p} bez tokenu`);
  }
});

test('walidacja', 'Wynik bez klucza, z błędnym kluczem lub tylko z tokenem administratora: 401', async (ctx) => {
  await seriesWithSensor(ctx);
  expectStatus(await send(ctx, null, { value: 1 }), 401, 'Wynik bez klucza');
  expectStatus(await send(ctx, 'x'.repeat(40), { value: 1 }), 401, 'Wynik z błędnym kluczem');
  expectStatus(await send(ctx, null, { value: 1 }, { token: await adminToken(ctx) }), 401, 'Wynik z tokenem administratora zamiast klucza');
});

test('walidacja', 'Wyników nie można modyfikować ani usuwać (404 lub 405)', async (ctx) => {
  const { key } = await seriesWithSensor(ctx);
  const m = await sendOk(ctx, key, { value: 7, timestamp: T0 });
  const t = await adminToken(ctx);
  for (const method of ['PUT', 'PATCH', 'DELETE']) {
    const body = method === 'DELETE' ? undefined : { value: 8, timestamp: T0 };
    expectStatus(await http(ctx, method, `/api/measurements/${m.id}`, { body, token: t }), [404, 405], `${method} /api/measurements/{id}`);
  }
  const after = await http(ctx, 'GET', `/api/measurements/${m.id}`);
  expect(after.status === 200 && after.json?.value === 7, 'Wynik został zmieniony lub usunięty');
});

test('walidacja', 'Sfałszowany token: 401', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/series', { body: seriesInput(), token: 'eyJhbGciOiJub25lIn0.eyJzdWIiOiJhZG1pbiJ9.' });
  if (res.status === 201) ctx.cleanup.series.add(res.json?.id);
  expectStatus(res, 401, 'POST ze sfałszowanym tokenem');
});

test('walidacja', 'Logowanie błędnym hasłem: 401', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/auth/login', { body: { username: ctx.user, password: ctx.pass + '-zle' } });
  expectStatus(res, 401, 'Logowanie błędnym hasłem');
});

test('walidacja', 'Zmiana hasła z błędnym bieżącym hasłem: 403; bez tokenu: 401', async (ctx) => {
  const body = { currentPassword: ctx.pass + '-zle', newPassword: 'NoweHaslo-123456' };
  expectStatus(await http(ctx, 'PUT', '/api/auth/password', { body, token: await adminToken(ctx) }), 403, 'Zmiana hasła z błędnym bieżącym hasłem');
  expectStatus(await http(ctx, 'PUT', '/api/auth/password', { body }), 401, 'Zmiana hasła bez tokenu');
});

// ---------------------------------------------------------------------------
// Grupa: konfiguracja bezpieczeństwa (A4)
// ---------------------------------------------------------------------------
test('bezpieczenstwo', 'Aplikacja i API działają przez HTTPS', async (ctx) => {
  expect(ctx.app.startsWith('https://') && ctx.api.startsWith('https://'), 'Adres aplikacji i API musi zaczynać się od https://');
});

test('bezpieczenstwo', 'Wejście przez http:// przekierowuje na https:// (lub port 80 jest zamknięty)', async (ctx) => {
  let res;
  try { res = await http(ctx, 'GET', ctx.app.replace(/^https:/, 'http:'), { redirect: 'manual' }); }
  catch { return; } // brak obsługi HTTP = brak treści przesyłanej otwartym tekstem
  expect([301, 302, 307, 308].includes(res.status) && (res.headers.get('location') || '').startsWith('https://'),
    `Oczekiwano przekierowania na https://, otrzymano ${res.status}`);
});

test('bezpieczenstwo', 'Strona główna wysyła nagłówek Content-Security-Policy', async (ctx) => {
  const res = await http(ctx, 'GET', ctx.app, { headers: { Accept: 'text/html' } });
  expectStatus(res, 200, 'GET strony głównej');
  expect(res.headers.get('content-security-policy'), 'Brak nagłówka Content-Security-Policy');
});

test('bezpieczenstwo', 'API wysyła X-Content-Type-Options: nosniff', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/series');
  expect((res.headers.get('x-content-type-options') || '').toLowerCase() === 'nosniff', 'Brak nagłówka X-Content-Type-Options: nosniff');
});

test('bezpieczenstwo', 'Ciasteczka ustawiane przy logowaniu mają HttpOnly, Secure i SameSite', async (ctx) => {
  const res = await http(ctx, 'POST', '/api/auth/login', { body: { username: ctx.user, password: ctx.pass } });
  for (const c of res.headers.getSetCookie?.() ?? []) {
    const low = c.toLowerCase();
    const name = c.split('=')[0];
    expect(low.includes('httponly'), `Ciasteczko ${name}: brak HttpOnly`);
    expect(low.includes('secure'), `Ciasteczko ${name}: brak Secure`);
    expect(low.includes('samesite'), `Ciasteczko ${name}: brak SameSite`);
  }
});

test('bezpieczenstwo', 'Odpowiedzi API nie ujawniają haseł, ich skrótów ani kluczy czujników', async (ctx) => {
  const { series, sensor, key } = await seriesWithSensor(ctx);
  await sendOk(ctx, key, { value: 1, timestamp: T0 });
  const t = await adminToken(ctx);
  const responses = [
    await http(ctx, 'POST', '/api/auth/login', { body: { username: ctx.user, password: ctx.pass } }),
    await http(ctx, 'GET', '/api/series'),
    await http(ctx, 'GET', '/api/sensors', { token: t }),
    await http(ctx, 'GET', `/api/sensors/${sensor.id}`, { token: t }),
    await http(ctx, 'GET', `/api/measurements?series=${series.id}`),
  ];
  for (const res of responses) {
    expect(!/"(password|passwordHash|password_hash|hash|salt|apiKeyHash|api_key_hash|keyHash)"\s*:/i.test(res.text), 'Odpowiedź zawiera pole z hasłem, kluczem lub ich skrótem');
    expect(!res.text.includes(ctx.pass), 'Odpowiedź zawiera hasło administratora');
    expect(!res.text.includes(key), 'Odpowiedź zawiera klucz API czujnika');
  }
});

test('bezpieczenstwo', 'CORS nie wpuszcza dowolnej domeny z poświadczeniami', async (ctx) => {
  const res = await http(ctx, 'GET', '/api/series', { headers: { Origin: 'https://evil.example' } });
  const acao = res.headers.get('access-control-allow-origin');
  const acac = (res.headers.get('access-control-allow-credentials') || '').toLowerCase() === 'true';
  expect(!(acac && (acao === 'https://evil.example' || acao === '*')), 'API odbija dowolny Origin razem z Allow-Credentials: true');
});

test('bezpieczenstwo', 'Odpowiedzi błędów nie zawierają śladów stosu', async (ctx) => {
  const r1 = await http(ctx, 'GET', '/api/series/999999999');
  const r2 = await http(ctx, 'POST', '/api/series', { rawBody: '{"name":', headers: { 'Content-Type': 'application/json' }, token: await adminToken(ctx) });
  const r3 = await send(ctx, 'x'.repeat(40), { value: 'abc' });
  for (const res of [r1, r2, r3]) {
    expect(!/(\n\s+at .+:\d+:\d+)|Traceback \(most recent|Exception in thread|"stack"\s*:/i.test(res.text), 'Odpowiedź błędu ujawnia ślad stosu lub szczegóły implementacji');
  }
});

// ---------------------------------------------------------------------------
// Lighthouse (A5) – wymaga Chrome/Chromium i dostępu do npx
// ---------------------------------------------------------------------------
function runLighthouse(url) {
  const r = spawnSync('npx', ['--yes', 'lighthouse', url, '--only-categories=accessibility', '--output=json',
    '--quiet', '--chrome-flags=--headless=new --no-sandbox'], { encoding: 'utf8', maxBuffer: 50 * 1024 * 1024, timeout: 180_000 });
  if (r.status !== 0) return { error: (r.stderr || 'Lighthouse nie uruchomił się').split('\n').slice(-3).join(' ') };
  try { return { score: Math.round(JSON.parse(r.stdout).categories.accessibility.score * 100) }; }
  catch { return { error: 'Nie udało się odczytać raportu Lighthouse' }; }
}
const lighthousePoints = (score) => (score >= 90 ? 3 : score >= 80 ? 2 : score >= 70 ? 1 : 0);

// ---------------------------------------------------------------------------
// Uruchomienie testów dla jednego zgłoszenia
// ---------------------------------------------------------------------------
async function warmUp(ctx) {
  for (let i = 0; i < WARMUP_TRIES; i++) {
    try { const r = await http(ctx, 'GET', '/api/health'); if (r.status < 500) return true; } catch { /* ponów */ }
    await new Promise((r) => setTimeout(r, 20_000));
  }
  return false;
}

async function cleanup(ctx) {
  try {
    const t = await adminToken(ctx);
    for (const id of ctx.cleanup.sensors) await http(ctx, 'DELETE', `/api/sensors/${id}`, { token: t }).catch(() => {});
    for (const id of ctx.cleanup.series) await http(ctx, 'DELETE', `/api/series/${id}`, { token: t }).catch(() => {});
  } catch { /* sprzątanie nie wpływa na wynik */ }
}

async function evaluate(sub, stage, { lighthouse, log }) {
  const ctx = {
    app: sub.app_url.replace(/\/+$/, ''), api: (sub.api_url || sub.app_url).replace(/\/+$/, ''),
    user: sub.username, pass: sub.password, token: null,
    cleanup: { series: new Set(), sensors: new Set() },
  };
  const cfg = SCORING[stage];
  const groups = { ...cfg.main, ...cfg.bonus };
  const results = [];

  if (!(await warmUp(ctx))) {
    log(`  ✘ Aplikacja niedostępna pod ${ctx.api}/api/health`);
    const points = Object.fromEntries(Object.keys(groups).map((g) => [g, 0]));
    return { points, total: 0, bonus: 0, results, unreachable: true };
  }

  for (const group of Object.keys(groups).filter((g) => g !== 'lighthouse')) {
    log(`\n[${CODES[group]}] ${group}`);
    for (const t of TESTS.filter((x) => x.group === group)) {
      let ok = true; let msg = '';
      try { await t.fn(ctx); } catch (e) {
        ok = false;
        if (e instanceof TestFailure) msg = e.message;
        else if (e instanceof TypeError && /undefined|null/.test(e.message)) msg = 'Nie wykonano: test zależy od wcześniejszego testu, który się nie powiódł';
        else if (e.name === 'TimeoutError') msg = `Brak odpowiedzi w ciągu ${TIMEOUT_MS / 1000} s`;
        else msg = `Błąd wykonania: ${e.message}`;
      }
      results.push({ group, name: t.name, ok, msg });
      log(`  ${ok ? '✔' : '✘'} ${t.name}${ok ? '' : `\n      → ${msg}`}`);
    }
  }
  await cleanup(ctx);

  const points = {};
  for (const [group, max] of Object.entries(groups)) {
    if (group === 'lighthouse') continue;
    const rs = results.filter((r) => r.group === group);
    points[group] = rs.length ? Math.round((max * rs.filter((r) => r.ok).length / rs.length) * 10) / 10 : 0;
  }
  if ('lighthouse' in groups) {
    if (lighthouse) {
      const lh = runLighthouse(ctx.app);
      points.lighthouse = lh.score === undefined ? 0 : lighthousePoints(lh.score);
      log(`\n[A5] Lighthouse Accessibility: ${lh.score ?? 'błąd'}${lh.error ? ` (${lh.error})` : ''} → ${points.lighthouse} pkt`);
    } else {
      points.lighthouse = null;
      log('\n[A5] Lighthouse pominięty (dodaj --lighthouse); sprawdź wynik w DevTools → Lighthouse');
    }
  }
  const total = Object.keys(cfg.main).reduce((s, g) => s + (points[g] ?? 0), 0);
  const bonus = Object.keys(cfg.bonus).reduce((s, g) => s + (points[g] ?? 0), 0);
  return { points, total: Math.round(total * 10) / 10, bonus, results };
}

// ---------------------------------------------------------------------------
// CSV i CLI
// ---------------------------------------------------------------------------
function parseCsv(text) {
  const rows = []; let row = []; let cell = ''; let q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
    else if (c === '"') q = true;
    else if (c === ',' || c === ';') { row.push(cell); cell = ''; }
    else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const [head, ...data] = rows.filter((r) => r.some((c) => c.trim()));
  return data.map((r) => Object.fromEntries(head.map((h, i) => [h.trim(), (r[i] ?? '').trim()])));
}
const csvCell = (v) => (/[",;\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v ?? ''));

async function main() {
  const { values: o } = parseArgs({
    options: {
      app: { type: 'string' }, api: { type: 'string' }, user: { type: 'string' }, pass: { type: 'string' },
      csv: { type: 'string' }, out: { type: 'string', default: 'wyniki.csv' },
      stage: { type: 'string', default: 'E2' }, lighthouse: { type: 'boolean', default: false },
      reports: { type: 'string', default: 'raporty' },
    },
  });
  const stage = o.stage.toUpperCase();
  if (!SCORING[stage]) { console.error('Nieznany etap; użyj --stage E1 lub --stage E2'); process.exit(2); }

  if (o.csv) {
    const subs = parseCsv(fs.readFileSync(o.csv, 'utf8'));
    fs.mkdirSync(o.reports, { recursive: true });
    const groups = Object.keys({ ...SCORING[stage].main, ...SCORING[stage].bonus });
    const lines = [['student', 'stage', ...groups.map((g) => `${CODES[g]} ${g}`), 'suma', 'bonus', 'uwagi'].map(csvCell).join(',')];
    for (const sub of subs) {
      console.log(`\n=== ${sub.student} (${sub.app_url})`);
      const buf = [];
      let r;
      try { r = await evaluate(sub, stage, { lighthouse: o.lighthouse, log: (s) => buf.push(s) }); }
      catch (e) { r = { points: {}, total: 0, bonus: 0, results: [], error: e.message }; }
      fs.writeFileSync(path.join(o.reports, `${sub.student.replace(/[^\w.-]+/g, '_')}-${stage}.txt`), buf.join('\n'));
      const note = r.unreachable ? 'aplikacja niedostępna' : r.error ?? '';
      lines.push([sub.student, stage, ...groups.map((g) => r.points[g] ?? ''), r.total, r.bonus, note].map(csvCell).join(','));
      console.log(`    suma ${r.total} pkt, bonus ${r.bonus} pkt ${note}`);
    }
    fs.writeFileSync(o.out, lines.join('\n') + '\n');
    console.log(`\nZapisano ${o.out} oraz raporty w katalogu ${o.reports}/`);
    return;
  }

  if (!o.app || !o.user || !o.pass) {
    console.error('Użycie: node zai-tests.mjs --app <URL> --user <login> --pass <hasło> [--api <URL>] [--stage E1|E2] [--lighthouse]');
    process.exit(2);
  }
  const r = await evaluate({ app_url: o.app, api_url: o.api, username: o.user, password: o.pass }, stage, { lighthouse: o.lighthouse, log: console.log });
  console.log(`\n=== Etap ${stage}: ${r.total} pkt (część automatyczna)` + (r.bonus ? `, bonus: ${r.bonus} pkt` : ''));
  for (const [g, p] of Object.entries(r.points)) console.log(`    ${CODES[g]} ${g}: ${p ?? '—'}`);
  console.log('Wynik jest orientacyjny; ocenę wystawia się na podstawie uruchomienia po terminie.');
}

main().catch((e) => { console.error(e); process.exit(1); });
