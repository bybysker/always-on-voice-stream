'use strict';
/*
 * The Voicestream app: setup wizard, Live, Meetings, Costs and Settings, on the app host's API
 * (voicestream/dashboard/app_routes.py and server.py) and its event stream (/api/events).
 *
 * Plain modern JavaScript with no build step and no external resources: the page's CSP allows
 * only this file, app.css and the inline bootstrap in app.html.
 *
 * Everything shown here can come from a transcript, a model or a tool (meeting titles, items,
 * notes, agent output, error messages), so it is all untrusted text: the page builds DOM nodes
 * and assigns textContent only. No HTML string is ever parsed — the notes' markdown renderer
 * below builds elements too, and only http(s)/mailto links survive it.
 */

const BOOT = window.VOICESTREAM_BOOT || { token: '', app: null };
const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;
const SVG_NS = 'http://www.w3.org/2000/svg';

// ======================================================================= DOM helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  add(el, children);
  return el;
}

function setAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'value') el.value = value;  // the property: what an input shows
    else if (key === 'checked') el.checked = Boolean(value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
}

/** Replace ``el``'s content; like h(), null/false/'' children are skipped (replaceChildren would print "null"). */
function fill(el, ...children) {
  el.replaceChildren();
  return add(el, children);
}

function add(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child == null || child === false || child === '') continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

// Stroke icons drawn here (no icon font or image: the CSP allows neither).
const ICONS = {
  mic: [['rect', { x: 9, y: 3, width: 6, height: 11, rx: 3 }], ['path', 'M5 11a7 7 0 0 0 14 0M12 18v3M8.5 21h7']],
  micOff: [['path', 'M15 9.5V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 4.6 2.5M5 11a7 7 0 0 0 11.3 5.5M19 11a7 7 0 0 1-.6 2.8M12 18v3M8.5 21h7M3 3l18 18']],
  meetings: [['rect', { x: 3, y: 4.5, width: 18, height: 16, rx: 2.5 }], ['path', 'M3 9.5h18M8 2.5v4M16 2.5v4M7.5 13.5h4M7.5 17h7']],
  costs: [['path', 'M4 20V11M10 20V5M16 20v-6M21 20H3']],
  settings: [['path', 'M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1'], ['circle', { cx: 15, cy: 6, r: 2 }], ['circle', { cx: 9, cy: 12, r: 2 }], ['circle', { cx: 17, cy: 18, r: 2 }]],
  play: [['path', 'M8 5.5v13l10.5-6.5z', true]],
  record: [['circle', { cx: 12, cy: 12, r: 6 }, true]],
  phone: [['path', 'M6.5 3.5h3l1.5 4.5-2 1.5a11 11 0 0 0 5.5 5.5l1.5-2 4.5 1.5v3a2 2 0 0 1-2 2A16 16 0 0 1 4.5 5.5a2 2 0 0 1 2-2z']],
  keyboard: [['rect', { x: 2.5, y: 6, width: 19, height: 12, rx: 2 }], ['path', 'M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M6 13.5h.01M17.5 13.5h.01M8.5 14.5h7']],
  pause: [['rect', { x: 6.5, y: 5, width: 3.5, height: 14, rx: 1 }, true], ['rect', { x: 14, y: 5, width: 3.5, height: 14, rx: 1 }, true]],
  stop: [['rect', { x: 6, y: 6, width: 12, height: 12, rx: 2 }, true]],
  check: [['path', 'M5 12.5l4.5 4.5L19 7.5']],
  checkCircle: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', 'M8 12.5l3 3 5-6']],
  x: [['path', 'M6 6l12 12M18 6L6 18']],
  xCircle: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', 'M9 9l6 6M15 9l-6 6']],
  alert: [['path', 'M10.3 4.2L2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z'], ['path', 'M12 9.5v4.5M12 17.5v.01']],
  info: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', 'M12 11v5.5M12 7.5v.01']],
  help: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', 'M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17.5v.01']],
  download: [['path', 'M12 4v11M7 10.5l5 5 5-5M5 20h14']],
  external: [['path', 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5']],
  folder: [['path', 'M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z']],
  refresh: [['path', 'M20 11a8 8 0 0 0-14.6-4.5M4 4v4.5h4.5M4 13a8 8 0 0 0 14.6 4.5M20 20v-4.5h-4.5']],
  mail: [['rect', { x: 3, y: 5, width: 18, height: 14, rx: 2.5 }], ['path', 'M3.5 7l8.5 6 8.5-6']],
  copy: [['rect', { x: 9, y: 9, width: 11, height: 11, rx: 2 }], ['path', 'M5 15V6a1 1 0 0 1 1-1h9']],
  back: [['path', 'M15 5l-7 7 7 7']],
  search: [['circle', { cx: 11, cy: 11, r: 6.5 }], ['path', 'M20 20l-4.2-4.2']],
  send: [['path', 'M4.5 12L20 4.5 13.5 20l-2-6.5z'], ['path', 'M11.5 13.5L20 4.5']],
  sparkle: [['path', 'M12 3.5l1.9 5.1L19 10.5l-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z'], ['path', 'M19 16v4M17 18h4']],
  shield: [['path', 'M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z'], ['path', 'M9 12l2.2 2.2L15.5 10']],
  user: [['circle', { cx: 12, cy: 8, r: 3.5 }], ['path', 'M5 20a7 7 0 0 1 14 0']],
  key: [['circle', { cx: 8, cy: 15, r: 4 }], ['path', 'M11 12l8.5-8.5M16 7l2.5 2.5M14 9l2 2']],
  tools: [['path', 'M14.5 6.5a4 4 0 0 0-5.4 5L3.5 17a1.8 1.8 0 0 0 2.5 2.5l5.5-5.6a4 4 0 0 0 5-5.4l-2.4 2.4-2.1-.4-.4-2.1z']],
  speaker: [['path', 'M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z'], ['path', 'M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11']],
  globe: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', 'M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18']],
  clock: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', 'M12 7v5l3 2']],
  trash: [['path', 'M4 7h16M10 11v6M14 11v6M6 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5L18 7M9 7V4.5h6V7']],
  plus: [['path', 'M12 5v14M5 12h14']],
  edit: [['path', 'M4 20h4L19 9a2.1 2.1 0 0 0-4-4L4 16z']],
  menu: [['path', 'M4 7h16M4 12h16M4 17h16']],
};

/** The product's mark: a voice as five bars, the one in the hue is what Voicestream keeps from it. */
function brandMark() {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'mark');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  [[3, 10, 14], [7.5, 6, 18], [12, 3, 21], [16.5, 7.5, 16.5], [21, 10.5, 13.5]].forEach(([x, y1, y2], i) => {
    const bar = document.createElementNS(SVG_NS, 'path');
    bar.setAttribute('d', `M${x} ${y1}V${y2}`);
    if (i === 2) bar.setAttribute('class', 'kept');
    svg.append(bar);
  });
  return svg;
}

function icon(name, cls = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  for (const [tag, spec, solid] of ICONS[name] || []) {
    const el = document.createElementNS(SVG_NS, tag);
    if (typeof spec === 'string') el.setAttribute('d', spec);
    else for (const [k, v] of Object.entries(spec)) el.setAttribute(k, String(v));
    if (solid) { el.setAttribute('fill', 'currentColor'); el.setAttribute('stroke', 'none'); }
    svg.append(el);
  }
  return svg;
}

function btn(label, attrs = {}, iconName = null) {
  const { cls = '', ...rest } = attrs;
  return h('button', { type: 'button', class: 'btn' + (cls ? ' ' + cls : ''), ...rest }, iconName && icon(iconName), label);
}

function spinner() { return h('span', { class: 'spin', 'aria-hidden': 'true' }); }

/** Redraw ``container`` with ``draw()`` without losing keyboard focus (the element keeps its id). */
function keepFocus(container, draw) {
  const active = document.activeElement;
  const id = active && container.contains(active) ? active.id : null;
  draw();
  if (id && $(id)) $(id).focus({ preventScroll: true });
}

// ============================================================================== i18n

let LANG = 'en';

/** The UI language: the ui_language preference, ``auto`` = the system's (navigator.language). */
function detectLang(pref) {
  if (pref === 'en' || pref === 'fr') return pref;
  const sys = (navigator.languages && navigator.languages[0]) || navigator.language || 'en';
  return /^fr\b/i.test(sys) ? 'fr' : 'en';
}

/** ``t('key', {n: 3})``: the string for the current language (English as the fallback). */
function t(key, vars) {
  let s = (STRINGS[LANG] && STRINGS[LANG][key]) ?? STRINGS.en[key];
  if (s == null) { console.warn('voicestream: missing string', key); return key; }
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? String(vars[k]) : m));
  return s;
}

/** Plural forms: ``tp('meetings.actions', 3)`` uses ``meetings.actions.one`` / ``.other``. */
function tp(key, n, vars) {
  const form = intl(Intl.PluralRules, LANG).select(n) === 'one' ? 'one' : 'other';
  return t(key + '.' + form, { n: fmtNum(n), ...vars });
}

function locale() {
  const sys = navigator.language || '';
  return sys.toLowerCase().startsWith(LANG) ? sys : LANG === 'fr' ? 'fr-FR' : 'en-US';
}

// ======================================================================== formatting

/** One Intl formatter per kind, locale and options, kept: making one is slow, and a long meeting's
 *  strip and lists format thousands of numbers. */
const INTL = new Map();
function intl(Kind, loc, opts) {
  const key = Kind.name + '|' + loc + '|' + (opts ? JSON.stringify(opts) : '');
  let f = INTL.get(key);
  if (!f) { f = new Kind(loc, opts); INTL.set(key, f); }
  return f;
}

function fmtNum(n) { return intl(Intl.NumberFormat, locale()).format(n); }

/** "this window, the menu bar or Spotlight", in the page's language. */
function fmtList(items, type = 'disjunction') {
  return Intl.ListFormat ? intl(Intl.ListFormat, locale(), { type }).format(items) : items.join(', ');
}

/** A figure with exactly ``digits`` decimals and no unit: "1.60", "1,60" (an axis whose unit is in its title). */
function fmtFixed(n, digits) {
  return intl(Intl.NumberFormat, locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
}

function money(x, digits) {
  const v = Number(x) || 0;
  const d = digits ?? (v > 0 && v < 0.01 ? 3 : 2);  // a fraction of a cent still shows as spent
  return intl(Intl.NumberFormat, locale(), { style: 'currency', currency: 'USD', minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
}

function fmtTs(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const pad = (n) => String(n).padStart(2, '0');
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`;
}

function toDate(iso) { const d = new Date(iso); return isNaN(d) ? null : d; }

function fmtClock(iso) {
  const d = toDate(iso);
  return d ? d.toLocaleTimeString(locale(), { hour: 'numeric', minute: '2-digit' }) : '';
}

function fmtDay(iso, opts) {
  const d = toDate(iso);
  return d ? d.toLocaleDateString(locale(), opts || { weekday: 'short', day: 'numeric', month: 'short' }) : '';
}

function fmtLongDate(iso) {
  return fmtDay(iso, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
}

/** "42 min", "1 h 05" — meeting lengths. */
function fmtDuration(seconds) {
  const m = Math.floor((seconds || 0) / 60);  // whole minutes, as the clock times beside them read
  if (m < 1) return t('dur.under_minute');
  if (m < 60) return t('dur.minutes', { n: m });
  return t('dur.hours', { h: Math.floor(m / 60), m: String(m % 60).padStart(2, '0') });
}

function fmtBytes(n) {
  const mb = (n || 0) / (1024 * 1024);
  if (mb >= 1024) return t('size.gb', { n: intl(Intl.NumberFormat, locale(), { maximumFractionDigits: 1 }).format(mb / 1024) });
  if (mb >= 1) return t('size.mb', { n: fmtNum(Math.round(mb)) });
  return t('size.mb', { n: intl(Intl.NumberFormat, locale(), { maximumFractionDigits: 1 }).format(mb) });
}

function fmtDue(item) {
  if (item.due_date) {
    const d = new Date(item.due_date + 'T12:00:00');
    if (!isNaN(d)) return d.toLocaleDateString(locale(), { weekday: 'short', day: 'numeric', month: 'short' });
  }
  return item.due_text || '';
}

/** Which heading a meeting goes under in the list: today, yesterday, this week, earlier. */
function dayBucket(iso, now = new Date()) {
  const d = toDate(iso);
  if (!d) return 'earlier';
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.floor((start - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return 'week';
  return 'earlier';
}

const SAFE_URL = /^(?:https?:\/\/|mailto:)[^\s<>"'`]+$/i;
function safeUrl(ref) {
  if (typeof ref !== 'string' || !SAFE_URL.test(ref)) return null;
  try { return new URL(ref).href; } catch (_) { return null; }
}
const basename = (p) => String(p || '').split(/[\\/]/).filter(Boolean).pop() || '';
function relPath(file, workspace) {
  return workspace && file.startsWith(workspace) ? file.slice(workspace.length).replace(/^[\\/]+/, '') : basename(file);
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// =============================================================================== API

class ApiError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body; }
}

async function api(method, path, body) {
  const init = { method, cache: 'no-store', headers: { 'X-Voicestream-Token': S.token } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch(path, init); } catch (_) { throw new ApiError(0, t('err.unreachable')); }
  return readJson(res);
}

async function readJson(res) {
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401) authFailed();
    throw new ApiError(res.status, (data && data.error) || res.statusText || String(res.status), data);
  }
  return data;
}

const BUSY = new WeakSet();

/** Run a request for a button (inactive meanwhile); a failure becomes a toast. The button is marked
 * aria-disabled rather than disabled: a disabled button drops the keyboard focus. */
async function act(button, fn) {
  if (button) {
    if (BUSY.has(button)) return undefined;  // pressed again while the first press is under way
    BUSY.add(button);
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-busy', 'true');
  }
  try { return await fn(); } catch (err) { toast(humanize(err.message), 'error'); return undefined; } finally {
    if (button) {
      BUSY.delete(button);
      button.removeAttribute('aria-disabled');
      button.removeAttribute('aria-busy');
    }
  }
}

// ============================================================= toasts and dialogs

/** ``action``: ``{label, run}`` (a button) or ``{label, href}`` (a link opened outside the app). */
function toast(text, kind = 'info', title = '', action = null) {
  const close = h('button', { type: 'button', class: 'x', 'aria-label': t('common.close') }, icon('x'));
  let actEl = null;
  if (action && action.href) {
    const url = safeUrl(action.href);
    if (url) actEl = h('a', { class: 'act', href: url, target: '_blank', rel: 'noopener noreferrer' }, action.label);
  } else if (action && action.run) {
    actEl = btn(action.label, { cls: 'small act', onclick: () => { el.remove(); action.run(); } });
  }
  const el = h('div', { class: 'toast ' + kind, role: kind === 'error' ? 'alert' : 'status' },
    h('div', { class: 'grow' }, title && h('strong', {}, title), text, actEl && h('div', {}, actEl)), close);
  close.addEventListener('click', () => el.remove());
  const box = $('toasts');
  el.dataset.key = kind + '\n' + title + '\n' + text;
  for (const old of [...box.children]) if (old.dataset.key === el.dataset.key) old.remove();  // the same news once
  box.append(el);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => el.remove(), kind === 'error' ? 12000 : action ? 9000 : 5000);
}

/** A confirmation in the page's own dialog (a native confirm() is not always available in the app window). */
function ask({ title, body = '', ok, cancel, danger = false }) {
  const dlg = $('ask');
  $('ask-title').textContent = title;
  $('ask-body').textContent = body;
  $('ask-ok').textContent = ok || t('common.ok');
  $('ask-ok').className = 'btn ' + (danger ? 'danger solid' : 'primary');
  $('ask-cancel').textContent = cancel || t('common.cancel');
  return new Promise((resolve) => {
    const done = (answer) => {
      $('ask-ok').onclick = $('ask-cancel').onclick = dlg.oncancel = null;
      if (dlg.open) dlg.close();
      resolve(answer);
    };
    $('ask-ok').onclick = () => done(true);
    $('ask-cancel').onclick = () => done(false);
    dlg.oncancel = (e) => { e.preventDefault(); done(false); };
    dlg.showModal();
    (danger ? $('ask-cancel') : $('ask-ok')).focus();
  });
}

async function viewFile(path) {
  try {
    const res = await fetch('/api/file?path=' + enc(path), { headers: { 'X-Voicestream-Token': S.token }, cache: 'no-store' });
    if (!res.ok) await readJson(res);
    $('viewer-body').textContent = await res.text();
    $('viewer-name').textContent = path;
    $('viewer').showModal();
    $('viewer-body').scrollTop = 0;
  } catch (err) {
    toast(err.message, 'error', t('file.cannot_open', { name: basename(path) }));
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    // older WebKit, or no permission: the classic way still works from a click
    const area = h('textarea', { class: 'sr-only', 'aria-hidden': 'true' });
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand && document.execCommand('copy');
    area.remove();
    if (!ok) throw new Error(t('err.copy'));
  }
}

// ============================================================================= state

const S = {
  token: BOOT.token,
  app: null,                 // GET /api/app, then every app.state event
  conn: 'connecting',        // the event stream: connecting | live | retry | dead
  authFailed: false,
  route: { name: 'live', parts: [] },
  userName: 'Me',
  live: {
    meeting: null,           // {id, title, started_at, folder}
    phase: 'idle',           // idle | live | ending | ended
    lastEnded: null,         // {id, title, folder, items} of the meeting that just ended
    sinks: [],
    sources: [],
    extraction: null,
    cost: null,
    items: new Map(),
    segs: [],                // transcript lines and screen notes, in order: [kind, data]
    seen: new Set(),
    partials: new Map(),     // source -> {speaker, text, at}
    meters: new Map(),       // source -> {el, fill, at}
    editing: new Set(),      // item ids with an open form: left alone while you type
    expanded: new Set(),
    follow: true,
    showDismissed: false,
  },
  lastMeeting: null,         // newest row of /api/meetings, for the idle Live screen
  lastItems: [],             // its to-dos and decisions
  lastSegments: [],          // and its transcript, for its voice strip
  recent: [],                // the newest rows of /api/meetings: the week's figures, the meetings before
  downloads: {},             // model name -> {done, total}
  models: null,              // GET /api/models
  settings: null,            // GET /api/settings
  devices: null,             // GET /api/devices
  draft: {},                 // unsaved settings: dotted path -> value
  prefsDraft: {},            // unsaved app preferences
  fieldErrors: {},           // dotted path -> message (a refused save)
  saveError: null,
  saving: false,
  lastMeetingLoaded: false,
  lastMeetingFailed: false,  // the last try failed: asked again on the next meeting event
  setupCompleted: false,
  heldStt: null,             // {name, changes}: a speech-to-text choice saved, sent once its model has downloaded
  meetings: { q: '', rows: [], total: 0, loaded: false, error: null },
  detail: null,              // {folder, data, tab, error, summarizing, pushing, showDismissed, editing, expanded, filter}
  costHours: 4,
  dismissed: new Set(),      // warnings hidden from the Live screen
  pushingFolder: null,
  setup: null,               // the wizard's state while it is open
  startAfterDownload: null,  // model name: start listening once it has downloaded
};

// ======================================================================= event stream

const EVENT_TYPES = [
  'state', 'app.state', 'app.warning', 'assistant.started', 'assistant.stopped', 'assistant.paused', 'assistant.resumed',
  'meeting.started', 'meeting.updated', 'meeting.ending', 'meeting.ended', 'meeting.notes_updated', 'meeting.saved',
  'segment', 'transcript.partial', 'level', 'extraction', 'item.added', 'item.updated', 'agent.updated', 'item.pushed',
  'sink.error', 'notes.pushed', 'cost', 'screen', 'source.error', 'source.stopped', 'agent.budget', 'agent.held',
  'model.download', 'settings.changed', 'app.focus',
];

let source = null, retryMs = 1000, retryTimer = null, deadSince = null;

function connect() {
  clearTimeout(retryTimer);
  if (source) source.close();
  const es = source = new EventSource('/api/events?token=' + enc(S.token));
  const onMessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    try { handle(e.type, ev); } catch (err) { console.error('voicestream: cannot handle', e.type, err); }
  };
  for (const type of EVENT_TYPES) es.addEventListener(type, onMessage);
  es.onerror = () => {
    if (S.authFailed) return es.close();
    setConn('retry');
    if (es.readyState !== EventSource.CLOSED) return;  // the browser retries by itself
    // it gave up (the app restarted, or an HTTP error): check the token, then retry with backoff
    clearTimeout(retryTimer);
    retryTimer = setTimeout(async () => {
      try { setApp(await api('GET', '/api/app')); } catch (err) { if (err.status === 401) return; }
      retryMs = Math.min(retryMs * 2, 15000);
      connect();
    }, retryMs);
  };
}

function setConn(conn) {
  if (S.conn === conn) return;
  S.conn = conn;
  deadSince = conn === 'live' ? null : deadSince || Date.now();
  renderConn();
  renderNotices();
}

function authFailed() {
  if (S.authFailed) return;
  S.authFailed = true;
  clearTimeout(retryTimer);
  if (source) source.close();
  setConn('dead');
}

// Host-side warnings and errors are worded for people by the host; a refetch picks them up.
const refreshApp = debounce(async () => {
  try { setApp(await api('GET', '/api/app')); } catch (_) { /* the stream reconnects by itself */ }
}, 400);

function handle(type, ev) {
  const d = ev.data || {};
  switch (type) {
    case 'state':
      retryMs = 1000;
      setConn('live');
      return applyLiveState(d);
    case 'app.state':
      return setApp(d);
    case 'app.focus':  // opened again (Spotlight, the Dock): the window is in front, its Start control in focus
      return focusStart();
    case 'app.warning':
    case 'source.error':
    case 'source.stopped':
      return refreshApp();
    case 'model.download':
      return onDownload(d);
    case 'settings.changed':
      return onSettingsChanged(d);
    case 'meeting.saved':
      return onMeetingSaved(d);
    case 'cost':
      return setCost(d);
    case 'assistant.started':
      return onAssistantStarted(d);
    case 'assistant.stopped':
      return onAssistantStopped(d);
    case 'sink.error':
      if (S.pushingFolder) return;  // the push's own result lists these
      return toast(humanize(d.error || ''), 'error', t('toast.push_failed', { tool: sinkLabel(d.sink) }));
    case 'meeting.notes_updated':
      onNotesUpdated(d);
      onDetailMeetingDone(d);
      break;
    case 'meeting.ended':
      onDetailMeetingDone(d);
      break;
  }
  if (d.item && S.detail && S.detail.data && d.meeting_id === S.detail.data.meeting.id) detailUpsert(d.item);
  liveEvent(type, d, ev);
}

// ============================================================================ app state

function setApp(st) {
  if (!st || !st.state) return;
  const before = S.app;
  // a reply and a pushed event come on different connections: the reply to Resume, made before a
  // meeting started a moment later, must not put "not in a meeting" back for the whole meeting
  if (before && st.boot === before.boot && st.rev < before.rev) return;
  S.app = st;
  if (before && before.prefs.ui_language !== st.prefs.ui_language) {
    const lang = detectLang(st.prefs.ui_language);
    if (lang !== LANG) return applyLanguage(lang);
  }
  if (S.setup && st.setup_done && !S.setup.forced) closeSetup();
  else if (!st.setup_done && !S.setup && !S.setupCompleted) openSetup();
  renderTopbar();
  renderNotices();
  renderNav();
  if (S.route.name === 'live') renderLive();
}

function stateKey() {
  const st = S.app ? S.app.state : 'stopped';
  return ['stopped', 'starting', 'listening', 'paused', 'stopping', 'error'].includes(st) ? st : 'stopped';
}

/** Meetings start when you start them (the default), or by themselves when someone speaks
 *  (``automatic``, the always-on listening of before): the words and controls follow it. */
function isManual() { return !S.app || S.app.mode !== 'automatic'; }

/** Where a meeting can be started from besides the shortcut, as this app has them: this window, and
 *  in the Mac app its menu bar, the floating button and Spotlight when they are on. */
function startPlaces() {
  const a = S.app || {};
  const caps = a.capabilities || {}, prefs = a.prefs || {};
  return [t('where.window'), caps.native_window && t('where.menu_bar'),
    caps.floating_button && prefs.floating_button && t('where.floating'),
    caps.spotlight && prefs.spotlight_launcher && t('where.spotlight')].filter(Boolean);
}

function meetingElapsed() {
  const m = (S.app && S.app.meeting) || (S.live.phase !== 'idle' && S.live.phase !== 'ended' ? S.live.meeting : null);
  if (!m || !m.started_at) return null;
  const start = toDate(m.started_at);
  return start ? Math.max(0, (Date.now() - start) / 1000) : null;
}

// Host messages (voicestream/app/host.py human_error and friends) are written in English; these
// are their French versions. Anything else is shown as it is.
const HOST_MESSAGES = [
  [/^Microphone permission is off/, 'hm.mic_off'],
  [/^Meeting audio isn't coming through/, 'hm.system_off'],
  [/^Speech-to-text with (\w+) needs your \1 key/, 'hm.stt_key'],
  [/^The speech-to-text model isn't downloaded yet/, 'hm.model_missing'],
  [/^Audio capture is missing from this install/, 'hm.no_audio'],
  [/^Speech-to-text on this Mac is missing from this install/, 'hm.no_whisper'],
  [/^Your settings file has a problem: (.*)$/s, 'hm.config'],
  [/^Listening stopped unexpectedly/, 'hm.stopped'],
  [/^no tools are enabled/, 'hm.no_tools'],
  [/^this model is in use/, 'hm.model_in_use'],
  [/^not listening$/, 'hm.not_listening'],
  [/^microphone delivers only digital silence/, 'hm.mic_silent'],
  [/^system audio has been pure digital silence/, 'hm.system_silent'],
  [/^download of \S+ interrupted \(([^)]*)\); resuming/, 'hm.download_retry', true],
  [/^download of (\S+) failed/, 'hm.download_failed', true],
  [/^Your microphone \((.+)\) isn't connected — using meeting audio only/, 'hm.mic_missing_only'],
  [/^Your microphone \((.+)\) isn't connected — nothing is recorded/, 'hm.mic_missing_none'],
  [/^Your microphone \((.+)\) isn't connected — using (.+) until it is back/, 'hm.mic_missing_default'],
  [/^The speech-to-text model is still downloading: listening starts/, 'hm.model_wait_start'],
  [/^The speech-to-text model is still downloading: Voicestream keeps/, 'hm.model_wait_switch'],
  [/^Your new settings aren't used yet: (.*)$/s, 'hm.not_applied'],
  [/^The Keychain didn't let Voicestream read (\S+):/, 'hm.keychain_denied'],
  // sinks/reminders.py PERMISSION_HELP is written for the terminal (Terminal, tccutil…)
  [/control Reminders \(error -1743\)/, 'hm.reminders_denied', true],
];

function humanize(msg) {
  const text = String(msg || '');
  for (const [re, key, always] of HOST_MESSAGES) {
    const m = text.match(re);
    if (m) {
      if (LANG === 'en' && key !== 'hm.not_listening' && !always) return text;  // already worded for people
      if (key === 'hm.not_applied') return t(key, { detail: humanize(m[1]) });  // the problem itself, in words too
      return t(key, { name: m[1] || '', detail: m[2] || m[1] || '' });
    }
  }
  return text;
}

/** The button that fixes a problem the host reported, if there is one. */
function fixFor(msg) {
  const text = String(msg || '');
  const mac = S.app && S.app.platform === 'darwin';
  if (/Privacy & Security → Microphone|microphone permission/i.test(text) && mac) {
    return { label: t('fix.open_mac_settings'), run: () => openPane('microphone') };
  }
  if (/Screen & System Audio Recording/i.test(text) && mac) {
    return { label: t('fix.open_mac_settings'), run: () => openPane('screen_audio') };
  }
  if (/Privacy & Security → Automation/i.test(text) && mac) {
    return { label: t('fix.open_mac_settings'), run: () => openPane('automation') };
  }
  if (/Settings → Speech-to-text|speech-to-text model|whisper-server|^download of /i.test(text)) {
    return { label: t('fix.stt_settings'), run: () => go('#/settings/stt') };
  }
  if (/Settings → Your tools/i.test(text)) return { label: t('fix.tools_settings'), run: () => go('#/settings/tools') };
  if (/ANTHROPIC|Claude key/i.test(text)) return { label: t('fix.claude_settings'), run: () => go('#/settings/claude') };
  if (/settings file has a problem/i.test(text)) return { label: t('fix.show_config'), run: () => reveal('config') };
  return null;
}

async function openPane(pane) {
  await act(null, () => api('POST', '/api/open-settings', { pane }));
}

async function reveal(what, folder) {
  await act(null, () => api('POST', '/api/reveal', folder ? { what, folder } : { what }));
}

// ================================================================== the shell itself

const SCREENS = ['live', 'meetings', 'costs', 'settings'];
const NAV_ICONS = { live: 'mic', meetings: 'meetings', costs: 'costs', settings: 'settings' };

function buildShell() {
  fill($('brand-mark'), brandMark());
  $('skip-link').textContent = t('common.skip');
  $('viewer-close').textContent = t('common.close');
  const menu = $('menu-btn');
  fill(menu, icon('menu'), h('span', { class: 'sr-only' }, t('nav.menu')));
  menu.onclick = () => setMenu(menu.getAttribute('aria-expanded') !== 'true');
  renderNav();
  renderConn();
  renderTopbar();
  renderNotices();
}

/** On a narrow window the sidebar is a bar at the top; its menu opens the rest of it as a sheet. */
function setMenu(open) {
  const menu = $('menu-btn');
  if (!menu || (menu.getAttribute('aria-expanded') === 'true') === open) return;
  menu.setAttribute('aria-expanded', open ? 'true' : 'false');
  fill(menu, icon(open ? 'x' : 'menu'), h('span', { class: 'sr-only' }, open ? t('common.close') : t('nav.menu')));
  document.body.classList.toggle('menu-open', open);
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.body.classList.contains('menu-open')) { setMenu(false); $('menu-btn').focus(); }
});

function renderNav() {
  const nav = $('nav');
  nav.setAttribute('aria-label', t('nav.label'));
  const dirty = Object.keys(S.draft).length + Object.keys(S.prefsDraft).length > 0;
  keepFocus(nav, () => fill(nav, ...SCREENS.map((name) => {
    const current = S.route.name === name;
    const a = h('a', { href: '#/' + name, id: 'nav-' + name, 'aria-current': current ? 'page' : null }, icon(NAV_ICONS[name]), h('span', {}, t('nav.' + name)));
    if (name === 'live' && S.app && S.app.in_meeting) {
      a.append(h('span', { class: 'live-dot', title: t('state.in_meeting') }), h('span', { class: 'sr-only' }, t('state.in_meeting')));
    }
    if (name === 'settings' && dirty) a.append(h('span', { class: 'badge-dot', title: t('set.unsaved') }));
    return a;
  })));
}

function renderConn() {
  const foot = $('sidebar-foot');
  if (!foot) return;
  const labels = { connecting: t('conn.connecting'), live: t('conn.live'), retry: t('conn.retry'), dead: t('conn.dead') };
  fill(foot,
    h('span', { class: 'conn ' + S.conn }, h('span', { class: 'dot' }), labels[S.conn]),
    S.app && h('span', { class: 'version' }, 'Voicestream ' + S.app.version),
  );
}

// ----------------------------------------------------------------------- top bar

function stateText() {
  const st = stateKey();
  // started by hand, listening is a recording: "Not recording" says what you need to know before a call
  if (isManual() && ['stopped', 'listening', 'stopping'].includes(st)) return t('state.m.' + st);
  return t('state.' + st);
}

/** The Live screen has the meeting's own controls when meetings start by hand: the recorder's are
 *  not shown twice there (it keeps saying what is going on). */
function liveOwnsControls() { return isManual() && S.route.name === 'live' && !S.setup; }

function renderTopbar() {
  const bar = $('topbar');
  if (!bar || !S.app) return;
  const st = stateKey();
  const a = S.app;
  // today's spending sits at the foot of the sidebar, under the screens it leads to (a link: it opens one)
  const spend = $('spend') || h('a', { class: 'spend', id: 'spend', href: '#/costs' });
  fillSpend(spend);
  if (!spend.isConnected) fill($('spend-slot'), spend);
  // the recorder is drawn again only when what it says changes: a screen reader hears its state once,
  // and a press in progress is not lost to a new button under the pointer
  const sig = [LANG, st, Boolean(a.in_meeting), a.error || '', isManual(), liveOwnsControls()].join('|');
  if (sig === bar.dataset.sig && bar.childElementCount) return;
  bar.dataset.sig = sig;
  const elapsed = meetingElapsed();
  const extra = h('span', { class: 'extra', id: 'state-extra' },
    a.in_meeting && elapsed != null ? t('state.meeting_for', { time: fmtDuration(elapsed) }) : '');
  const pill = h('div', { class: 'state st-' + st, role: 'status', 'aria-live': 'polite', id: 'state-pill' },
    h('span', { class: 'dot-state', 'aria-hidden': 'true' }),
    h('span', { class: 'text' }, stateText()), extra);
  if (st === 'error' && a.error) pill.title = humanize(a.error);

  const manual = isManual();
  const busy = st === 'starting' || st === 'stopping';
  let primary;
  if (st === 'listening') primary = btn(t('action.pause'), { cls: 'big', id: 'primary', onclick: (e) => listen('pause', e.currentTarget) }, 'pause');
  else if (st === 'paused') primary = btn(t('action.resume'), { cls: 'primary big', id: 'primary', onclick: (e) => listen('resume', e.currentTarget) }, 'play');
  else if (busy) primary = h('button', { type: 'button', class: 'btn primary big', id: 'primary', disabled: true }, spinner(), stateText());
  else primary = btn(manual ? t('action.start_meeting') : t('action.start'), { cls: 'primary big', id: 'primary', onclick: (e) => meetingStart(e.currentTarget) }, manual ? 'record' : 'play');

  const canStop = ['listening', 'paused', 'starting', 'stopping', 'error'].includes(st) && !(st === 'error' && !a.error);
  const stop = st === 'stopping'
    ? btn(t('action.stop_now'), { cls: 'danger', id: 'stop', onclick: (e) => stopNow(e.currentTarget) })
    : btn(st === 'error' ? t('action.dismiss') : t('action.stop'), { id: 'stop', onclick: (e) => stopListening(e.currentTarget) }, st === 'error' ? null : 'stop');
  stop.hidden = !canStop || (st === 'error' && !a.error);

  const actions = h('div', { class: 'recorder-actions', hidden: liveOwnsControls() }, primary, stop);
  keepFocus(bar, () => fill(bar, pill, actions));
}

function fillSpend(el) {
  const c = S.live.cost;
  const spent = c && c.today ? c.today.total_usd ?? ((c.today.stt_usd || 0) + (c.today.llm_usd || 0) + (c.today.agents_usd || 0)) : (S.app ? S.app.today_usd : 0);
  const budget = c ? c.daily_budget_usd : null;
  const frac = budget ? ((c.today.stt_usd || 0) + (c.today.llm_usd || 0)) / budget : 0;
  el.className = 'spend' + (frac >= 1 ? ' over' : frac >= 0.8 ? ' warn' : '');
  el.setAttribute('aria-label', t('top.spend_aria', { amount: money(spent) }));
  const track = h('span', { class: 'track', 'aria-hidden': 'true' }, h('span'));
  track.firstChild.style.width = Math.min(100, frac * 100).toFixed(1) + '%';
  fill(el, h('span', {}, t('top.today')), h('strong', {}, money(spent)), budget ? track : null);
  el.title = c && c.today ? [
    t('cost.stt') + ' ' + money(c.today.stt_usd),
    t('cost.claude') + ' ' + money(c.today.llm_usd),
    t('cost.agents') + ' ' + money(c.today.agents_usd),
    budget ? t('cost.budget_line', { amount: money(budget) }) : '',
  ].filter(Boolean).join('\n') : t('top.spend_title');
}

async function listen(action, button) {
  const res = await act(button, () => api('POST', '/api/listen/' + action));
  if (res) setApp(res);
}

/** Start, Stop and Keep go through the host's meeting actions, the ones the menu bar, the shortcut, the
 *  floating button and Spotlight use too (POST /api/meeting/…): a start after a problem is a restart,
 *  as the problem can leave half of it running (the microphone gave up, meeting audio still recorded). */
async function meetingAction(what, button) {
  const res = await act(button, () => api('POST', '/api/meeting/' + what));
  if (res) setApp(res);
  return res;
}

function meetingStart(button) { return meetingAction('start', button); }

/** Stop. A meeting started by hand ends as with the shortcut or the floating button, without a question:
 *  Stop is how it is meant to end (its notes are written as usual). Listening all the time, Stop is not
 *  the usual end of a meeting (silence is), so it is checked first. */
async function stopListening(button) {
  if (stateKey() === 'error' && !S.app.in_meeting) return meetingAction('stop', button);  // "Dismiss"
  if (S.app.in_meeting && !isManual()) {
    const ok = await ask({ title: t('ask.stop_title'), body: t('ask.stop_body'), ok: t('action.stop'), danger: false });
    if (!ok) return;
  }
  return meetingAction('stop', button);
}

/** A call, "Still in a meeting?", or something that happened by itself: Start / Not now, Keep, OK. */
async function answer(path, button) {
  const res = await act(button, () => api('POST', '/api/' + path));
  if (res) setApp(res);
  return res;
}

async function stopNow(button) {
  const ok = await ask({ title: t('ask.stop_now_title'), body: t('ask.stop_now_body'), ok: t('action.stop_now'), danger: true });
  if (!ok) return;
  const res = await act(button, () => api('POST', '/api/listen/stop?force=1'));
  if (res) setApp(res);
}

// ----------------------------------------------------------------------- notices

function notice(kind, iconName, msg, ...actions) {
  return h('div', { class: 'notice ' + kind, role: kind === 'error' ? 'alert' : 'status' },
    icon(iconName), h('div', { class: 'msg' }, msg), ...actions);
}

/** Shell-wide notices, on every screen: the connection, new settings waiting for the meeting to end, and
 *  what the app asks or tells from its state: a call that may want notes, "Still in a meeting?", a
 *  meeting that ended by itself. Inline, never a dialog: nothing takes the focus from what you are doing.
 *  A notice already on show is kept as it is (a screen reader hears it once, a focused button stays). */
function renderNotices() {
  const box = noticesBox();
  if (!box) return;
  const out = [];  // [key, make]
  if (S.authFailed) {
    out.push(['auth', () => notice('error', 'alert', t('conn.auth'))]);
  } else if (S.conn !== 'live' && deadSince && Date.now() - deadSince > 3000) {
    out.push(['conn', () => notice('warn', 'refresh', t('conn.lost'))]);
  }
  const a = S.app;
  if (a && a.setup_done && !S.setup) {
    const s = a.suggestion, still = a.still_there, n = a.notice;
    if (s) out.push(['suggestion|' + s.at + '|' + (s.app || ''), () => suggestionBanner(s)]);
    if (still) out.push(['still|' + still.meeting_id + '|' + still.silent_minutes + '|' + (still.ends_at || ''), () => stillThereBanner(still)]);
    // drawn again when it changes, the notes being written included (an empty meeting's folder stays empty)
    if (n && NOTICE_KINDS.includes(n.kind)) {
      const how = n.pending === true ? 'p' : n.unfinished === true ? 'u' : '';
      out.push(['notice|' + n.kind + '|' + (n.at || '') + '|' + how + '|' + (n.folder || '') + '|' + (n.title || ''),
        () => appNoticeBanner(n)]);
    }
  }
  if (a && a.restart_pending) {
    out.push(['restart', () => notice('info', 'info', t('notice.restart_pending'),
      btn(t('notice.apply_now'), { cls: 'small', id: 'apply-now', onclick: (e) => applyNow(e.currentTarget) }))]);
  }
  const shown = new Map([...box.children].map((el) => [el.dataset.key, el]));
  const want = out.map(([key, make]) => {
    const k = LANG + '|' + key;
    const el = shown.get(k) || make();
    el.dataset.key = k;
    return el;
  });
  want.forEach((el, i) => { if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null); });
  while (box.children.length > want.length) box.lastElementChild.remove();
}

const NOTICE_KINDS = ['ended_after_silence', 'link_refused'];

let NOTICES = null;
/** The shell's notices, kept by reference: in a meeting they are part of Live's view (placeNotices). */
function noticesBox() { return NOTICES || (NOTICES = $('notices')); }

/** In a meeting on Live, the shell's banners come under its title and controls, as Live's own warnings
 *  do (attachLive): one that comes or goes by itself ("Still in a meeting?" goes as soon as someone
 *  speaks) moves the panes, never Pause or Stop away from a pointer on its way to them, nor End meeting
 *  under it. Everywhere else, above the screen. */
function placeNotices() {
  const box = noticesBox();
  if (!box) return;
  const inMeeting = S.route.name === 'live' && !S.setup && LV.root && LV.meeting && LV.meeting.parentNode === LV.root;
  if (inMeeting) {
    if (box.previousElementSibling !== LV.head) LV.head.after(box);
  } else {
    homeNotices();
  }
}

function homeNotices() {
  const box = noticesBox(), main = $('main');
  if (box && main && box.nextElementSibling !== main) main.before(box);
}

/** "Call detected in Zoom. Start notes for this call?": Start meeting / Not now. Never a start by itself. */
function suggestionBanner(s) {
  const title = s.app ? t('sug.title', { app: s.app }) : t('sug.title_any');
  return notice('info call', 'phone', h('span', {}, h('strong', {}, title), ' ', t('sug.text')),
    h('div', { class: 'notice-actions' },
      btn(t('sug.dismiss'), { cls: 'small ghost', id: 'suggestion-dismiss', onclick: (e) => answer('suggestion/dismiss', e.currentTarget) }),
      btn(t('action.start_meeting'), { cls: 'small', id: 'suggestion-start', onclick: (e) => answer('suggestion/accept', e.currentTarget) }, 'record')));
}

/** A long silence in a meeting started by hand: keep recording (the counts start over), or end it now. */
function stillThereBanner(s) {
  const until = s.ends_at && fmtClock(s.ends_at);
  return notice('info', 'clock', h('span', {}, h('strong', {}, t('still.title')), ' ',
    t('still.text', { n: fmtNum(s.silent_minutes || 0) }), until ? ' ' + t('still.ends', { time: until }) : ''),
  h('div', { class: 'notice-actions' },
    btn(t('live.end'), { cls: 'small ghost', id: 'still-end', onclick: (e) => meetingAction('stop', e.currentTarget) }),
    btn(t('still.keep'), { cls: 'small', id: 'still-keep', onclick: (e) => meetingAction('keep', e.currentTarget) })));
}

/** Something that happened by itself and should be known: the meeting ended after a long silence (its
 *  notes are being written, then ready, or there were none — or its wrap-up never finished, cut short
 *  or failed, and nobody knows), or a link asked to record without Voicestream's key. OK puts it away. */
function appNoticeBanner(n) {
  const ok = btn(t('common.ok'), { cls: 'small', id: 'notice-ok', onclick: (e) => answer('notice/dismiss', e.currentTarget) });
  if (n.kind === 'link_refused') {
    return notice('warn', 'shield', h('span', {}, h('strong', {}, t('notice.link_title')), ' ', t('notice.link_text')),
      h('div', { class: 'notice-actions' }, ok));
  }
  const mins = fmtNum(n.minutes || 0);
  // until the meeting is wrapped up nobody knows whether it has notes: not "nothing was said" meanwhile,
  // nor after a wrap-up that never said (unfinished): only that it ended
  const writing = n.pending === true, unfinished = !writing && n.unfinished === true;
  const text = writing ? t('notice.silence_writing', { n: mins }) : unfinished ? t('notice.silence_unfinished', { n: mins })
    : !n.folder ? t('notice.silence_empty', { n: mins })
    : n.title ? t('notice.silence_text', { title: n.title, n: mins }) : t('notice.silence_untitled', { n: mins });
  // a link (it opens a screen), which also puts the notice away
  const open = !writing && !unfinished && n.folder && h('a', { class: 'btn small ghost', id: 'notice-open', href: '#/meetings/' + enc(n.folder),
    onclick: () => { answer('notice/dismiss', null); } }, t('live.open_notes'));
  return notice('info', 'checkCircle', h('span', {}, h('strong', {}, t('notice.silence_title')), ' ', text),
    h('div', { class: 'notice-actions' }, open, ok));
}

async function applyNow(button) {
  const ok = await ask({ title: t('ask.apply_title'), body: t('ask.apply_body'), ok: t('notice.apply_now') });
  if (!ok) return;
  const res = await act(button, () => api('POST', '/api/listen/restart'));
  if (res) setApp(res);
}

// ------------------------------------------------------------------------ router

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch (_) { return p; }
  });
  const name = SCREENS.includes(parts[0]) ? parts[0] : 'live';
  return { name, parts: parts.slice(1) };
}

function go(hash) {
  if (S.setup && !S.setup.forced) return;
  if (S.setup) closeSetup();
  if (location.hash === hash) navigate(true);
  else location.hash = hash;
}

/** Show the screen in the address (``#/meetings/<folder>``…); ``focus`` moves keyboard focus to it. */
function navigate(focus) {
  const prev = S.route;
  S.route = parseRoute();
  placeNotices();  // out of Live's meeting before the screens not on show are emptied
  for (const name of SCREENS) {
    const screen = $('screen-' + name);
    screen.hidden = name !== S.route.name;
    // only the screen on show is in the page: each is drawn again when it is opened (Live keeps its
    // parts, and follows the meeting meanwhile), and a hidden screen's title isn't the page's title
    if (screen.hidden) screen.replaceChildren();
  }
  document.body.classList.remove('raise-toasts', 'live-meeting');  // the save bar and the live meeting put them back
  setMenu(false);
  renderNav();
  renderTopbar();  // on Live, the recorder leaves the meeting's controls to the screen
  const r = S.route;
  if (r.name === 'live') renderLive();
  else if (r.name === 'meetings') renderMeetings(prev);
  else if (r.name === 'costs') renderCosts();
  else if (r.name === 'settings') renderSettings();
  document.title = t('nav.' + r.name) + ' · Voicestream';
  if (focus) {
    const main = $('main');
    if (prev.name !== r.name || prev.parts.join('/') !== r.parts.join('/')) window.scrollTo(0, 0);
    const section = prev.name === 'settings' && r.name === 'settings' && $('screen-settings').querySelector('.subnav [aria-current=page]');
    const control = r.name === 'live' && (liveFocusTarget() || (S.focusStart && $('primary')));
    S.focusStart = false;
    if (r.name === 'settings' && settingsAnchor()) {
      // a link to one section of a screen ("Change" beside how meetings start): that section, in view
      focusSettingsAnchor();
    } else if (section) {
      section.focus({ preventScroll: true });  // moving along the sections: stay in the section list
    } else if (control) {
      focusLive(control);  // Live's one job: Return starts the meeting
    } else {
      const heading = $('screen-' + r.name).querySelector('h1');
      (heading || main).setAttribute('tabindex', '-1');
      (heading || main).focus({ preventScroll: true });
    }
  } else if (settingsAnchor()) {
    focusSettingsAnchor();  // the window opened on that link
  }
}

window.addEventListener('hashchange', () => navigate(true));

// --------------------------------------------------------------------- every second

function tick() {
  const elapsed = meetingElapsed();
  const extra = $('state-extra');
  if (extra) extra.textContent = S.app && S.app.in_meeting && elapsed != null ? t('state.meeting_for', { time: fmtDuration(elapsed) }) : '';
  if (LV.elapsed && S.live.meeting) LV.elapsed.textContent = elapsed != null ? fmtTs(elapsed) : '';
  if (LV.clockTime && S.live.meeting && stateKey() !== 'stopping') LV.clockTime.textContent = elapsed != null ? fmtTs(elapsed) : '';
  // a minute at a time: each new length lays out every bar of the strip again
  if (LV.voices && inMeetingView()) LV.voices.setTotal(Math.ceil((elapsed || 0) / 60) * 60);
  const now = Date.now();
  for (const m of S.live.meters.values()) {
    if (now - m.at > 2500) { m.fill.style.transform = 'scaleX(0)'; m.el.classList.remove('speech'); }
  }
  let stale = false;
  for (const [src, p] of S.live.partials) {
    if (now - p.at > 10000) { S.live.partials.delete(src); stale = true; }
  }
  if (stale) renderPartials();
  if (S.conn !== 'live' && deadSince && now - deadSince > 3000 && !noticesBox().querySelector(`[data-key="${LANG}|conn"]`)) renderNotices();
}

// ======================================================================= item cards

const KINDS = ['action', 'decision', 'question', 'note'];
const RUN_LABELS = { suggested: 'agent.run', failed: 'agent.retry', rejected: 'agent.run_anyway', done: 'agent.run_again' };

function isMine(owner) {
  const o = String(owner || '').trim().toLowerCase();
  if (!o) return false;
  const me = String(S.userName || '').trim().toLowerCase();
  return o === me || o === 'me' || o === 'moi' || (me && o.split(/\s+/)[0] === me.split(/\s+/)[0] && me.includes(' '));
}

/**
 * The items of one meeting, grouped by kind, as cards. Used by the Live screen (``ctx.live``: push and
 * agent buttons, through ``/api/items``) and a past meeting's Actions tab (``/api/meetings/<folder>/items``).
 *
 * ctx: ``{path(item, action), live, sinks(), readOnly(), items: Map, editing: Set, expanded: Set}``.
 */
class ItemBoard {
  constructor(ctx) {
    this.ctx = ctx;
    this.items = ctx.items;
    this.cards = new Map();
    this.groups = {};
    this.el = h('div', { class: 'groups' + (ctx.showDismissed ? ' show-dismissed' : '') });
    for (const kind of KINDS) {
      const cards = h('div'), count = h('span', { class: 'count' }, '0'), none = h('p', { class: 'none' }, t('items.none'));
      const head = h('h3', { id: `g-${ctx.id}-${kind}` }, t('kind.' + kind + '.plural'), count);
      this.el.append(h('section', { class: 'group', 'aria-labelledby': head.id, dataset: { kind } }, head, cards, none));
      this.groups[kind] = { cards, count, none };
    }
    for (const item of this.items.values()) this.render(item);
    this.counts();
  }

  set(item) {
    if (!item || !item.id) return;
    const old = this.items.get(item.id);
    if (old && (item.revision || 0) < (old.revision || 0)) return;  // an older snapshot
    this.items.set(item.id, item);
    this.render(item);
  }

  render(item) {
    if (this.ctx.editing.has(item.id)) return;  // re-rendered when the form closes
    const card = buildItemCard(item, this);
    const old = this.cards.get(item.id);
    if (old && old.isConnected) old.replaceWith(card);
    else (this.groups[item.kind] || this.groups.note).cards.append(card);
    this.cards.set(item.id, card);
    this.counts();
    if (this.ctx.onRender) this.ctx.onRender(item);
  }

  renderAll() { for (const item of this.items.values()) this.render(item); }

  clear() {
    this.items.clear();
    this.cards.clear();
    for (const g of Object.values(this.groups)) fill(g.cards);
    this.counts();
  }

  counts() {
    const all = this.el.classList.contains('show-dismissed');
    for (const [kind, g] of Object.entries(this.groups)) {
      const items = [...this.items.values()].filter((i) => (KINDS.includes(i.kind) ? i.kind : 'note') === kind);
      const visible = items.filter((i) => !i.dismissed).length;
      g.count.textContent = fmtNum(visible);
      g.none.hidden = (all ? items.length : visible) > 0;
    }
  }

  showDismissed(on) {
    this.ctx.showDismissed = on;
    this.el.classList.toggle('show-dismissed', on);
    this.counts();
  }

  closeForm(id) {
    this.ctx.editing.delete(id);
    const item = this.items.get(id);
    if (item) this.render(item);
  }

  async patch(item, changes, button) {
    const res = await act(button, () => api('PATCH', this.ctx.path(item), changes));
    if (res) this.set(res);
    return res;
  }

  async dismiss(item, button) {
    const res = await act(button, () => api('POST', this.ctx.path(item, item.dismissed ? 'restore' : 'dismiss')));
    if (res && res.id) this.set(res);  // past meetings answer with the item; live ones send item.updated
  }
}

function buildItemCard(item, board) {
  const cls = 'item' + (item.dismissed ? ' dismissed' : '') + (item.status === 'done' ? ' done' : '');
  return h('article', { class: cls, dataset: { id: item.id }, 'aria-label': item.title },
    h('div', { class: 'item-top' },
      h('span', { class: 'title' }, item.title),
      (item.confidence ?? 1) < 0.6 && h('span', { class: 'pill warn', title: t('item.unsure_help') }, t('item.unsure'))),
    itemMeta(item),
    itemBadges(item),
    itemEvidence(item, board),
    item.agent && agentBlock(item, board),
    itemActions(item, board));
}

function itemMeta(item) {
  const due = fmtDue(item);
  const mine = isMine(item.owner);
  const bits = [
    item.owner && person(item.owner, mine),
    due && h('span', { title: [item.due_text, item.due_date].filter(Boolean).join(' · ') }, t('item.due', { when: due })),
    item.priority && h('span', { class: 'prio-' + item.priority }, t('prio.' + item.priority)),
    item.status === 'done' && h('span', {}, t('item.status_done')),
    item.status === 'cancelled' && h('span', {}, t('item.status_cancelled')),
    item.dismissed && h('span', {}, t('item.dismissed')),
  ].filter(Boolean);
  return bits.length ? h('div', { class: 'meta' }, bits) : null;
}

function itemBadges(item) {
  const pushed = Object.entries(item.pushed || {});
  if (!pushed.length) return null;
  return h('div', { class: 'badges' }, pushed.map(([sink, ref]) => {
    const url = safeUrl(ref);
    const label = [icon('check'), sinkLabel(sink)];
    return url
      ? h('a', { class: 'badge', title: ref, href: url, target: '_blank', rel: 'noopener noreferrer' }, label)
      : h('span', { class: 'badge', title: ref || sink }, label);
  }));
}

function itemEvidence(item, board) {
  if (!item.evidence && !item.detail) return null;
  const box = h('details', { class: 'evidence' },
    h('summary', {}, item.evidence ? t('item.evidence') : t('item.details')),
    item.detail && h('p', {}, item.detail),
    item.evidence && h('blockquote', {}, '“' + item.evidence + '”'));
  box.open = board.ctx.expanded.has(item.id);
  box.addEventListener('toggle', () => (box.open ? board.ctx.expanded.add(item.id) : board.ctx.expanded.delete(item.id)));
  return box;
}

function agentBlock(item, board) {
  const a = item.agent, status = a.status;
  const box = h('div', { class: 'agent' },
    h('div', { class: 'agent-head' },
      h('span', { class: 'hint' }, t('agent.label')),
      h('span', { class: 'chip ' + status }, t('agent.status.' + status)),
      a.explicit && h('span', { class: 'hint' }, t('agent.you_asked')),
      status === 'running' && a.started_at && h('span', { class: 'hint' }, t('agent.since', { time: fmtClock(a.started_at) })),
      a.cost_usd > 0 && h('span', { class: 'hint' }, money(a.cost_usd))),
    status === 'suggested' && a.rationale && h('p', { class: 'hint' }, a.rationale),
    a.result_summary && h('div', { class: 'summary' }, a.result_summary),
    a.error && h('p', { class: 'error-text' }, a.error),
    (a.result_files || []).length > 0 && h('div', { class: 'files' }, a.result_files.map((f) =>
      h('button', { type: 'button', class: 'btn link', title: f, onclick: () => viewFile(f) }, relPath(f, a.workspace)))));
  if (!board.ctx.live || board.ctx.readOnly()) return box;
  const buttons = [
    RUN_LABELS[status] && btn(t(RUN_LABELS[status]) + '…', {
      cls: 'small' + (status === 'suggested' ? ' primary' : ''), onclick: () => openPrompt(item, box, board),
    }),
    (status === 'suggested' || status === 'failed') && btn(t('agent.reject'), {
      cls: 'small', onclick: (e) => act(e.currentTarget, () => api('POST', board.ctx.path(item, 'agent/reject'))),
    }),
  ].filter(Boolean);
  if (buttons.length) box.append(h('div', { class: 'item-actions' }, buttons));
  return box;
}

function openPrompt(item, box, board) {
  board.ctx.editing.add(item.id);
  const brief = h('textarea', { 'aria-label': t('agent.brief') });
  brief.value = item.agent.prompt || '';
  const run = h('button', { type: 'submit', class: 'btn small primary' }, t('agent.run_now'));
  const form = h('form', { class: 'prompt' },
    h('span', { class: 'hint' }, t('agent.brief_help')), brief,
    h('div', { class: 'item-actions' }, run, btn(t('common.cancel'), { cls: 'small', onclick: () => board.closeForm(item.id) })));
  form.addEventListener('keydown', (e) => { if (e.key === 'Escape') board.closeForm(item.id); });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const current = (board.items.get(item.id) || item).agent || {};
    const prompt = brief.value.trim();
    act(run, async () => {
      await api('POST', board.ctx.path(item, 'agent/run'), prompt && prompt !== current.prompt ? { prompt } : {});
      board.closeForm(item.id);
    });
  });
  fill(box, form);
  brief.focus();
}

function itemActions(item, board) {
  const ctx = board.ctx;
  if (ctx.readOnly()) return null;
  const sinks = ctx.live ? ctx.sinks() : null;
  const pushedTo = Object.keys(item.pushed || {});
  const done = item.status === 'done';
  return h('div', { class: 'item-actions' },
    !item.dismissed && item.kind === 'action' && btn(done ? t('item.reopen') : t('item.mark_done'), {
      cls: 'small', onclick: (e) => board.patch(item, { status: done ? 'open' : 'done' }, e.currentTarget),
    }, done ? null : 'check'),
    !item.dismissed && sinks && btn(pushedTo.length ? t('item.push_again') : t('item.push'), {
      cls: 'small', disabled: !sinks.length,
      title: sinks.length ? t('item.push_to', { tools: sinks.map(sinkLabel).join(', ') }) : t('item.no_tools'),
      onclick: (e) => pushItem(item, board, e.currentTarget),
    }, 'send'),
    !item.dismissed && btn(t('item.edit'), { cls: 'small', onclick: () => openEdit(item, board) }, 'edit'),
    btn(item.dismissed ? t('item.restore') : t('item.dismiss'), { cls: 'small', onclick: (e) => board.dismiss(item, e.currentTarget) }));
}

function pushItem(item, board, button) {
  act(button, async () => {
    const { results } = await api('POST', board.ctx.path(item, 'push'));
    const entries = Object.entries(results || {});
    const ok = entries.filter(([, ref]) => ref !== 'error').map(([sink]) => sinkLabel(sink));
    if (ok.length) toast(t('toast.pushed_item', { id: item.id, tools: ok.join(', ') }), 'ok', t('toast.pushed'));
    else if (!entries.length) toast(t('toast.no_tool_takes', { kind: t('kind.' + item.kind + '.one').toLowerCase() }), 'warn');
    // failures arrive as sink.error events, with the reason
  });
}

function openEdit(item, board) {
  const card = board.cards.get(item.id);
  if (!card) return;
  board.ctx.editing.add(item.id);
  const title = h('input', { value: item.title, 'aria-label': t('item.title'), required: true, maxlength: 200 });
  const owner = h('input', { value: item.owner || '', placeholder: t('item.owner'), 'aria-label': t('item.owner'), maxlength: 80 });
  const due = h('input', { value: item.due_text || item.due_date || '', placeholder: t('item.due_ph'), 'aria-label': t('item.due_label'), maxlength: 80 });
  const save = h('button', { type: 'submit', class: 'btn small primary' }, t('common.save'));
  const form = h('form', { class: 'edit' },
    title, h('div', { class: 'row' }, owner, due),
    h('div', { class: 'item-actions' }, save, btn(t('common.cancel'), { cls: 'small', onclick: () => board.closeForm(item.id) })));
  form.addEventListener('keydown', (e) => { if (e.key === 'Escape') board.closeForm(item.id); });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const cur = board.items.get(item.id) || item;
    const changes = {};
    if (title.value.trim() && title.value.trim() !== cur.title) changes.title = title.value.trim();
    if (owner.value.trim() !== (cur.owner || '')) changes.owner = owner.value.trim();
    if (due.value.trim() !== (cur.due_text || cur.due_date || '')) changes.due_text = due.value.trim();
    if (!Object.keys(changes).length) return board.closeForm(item.id);
    act(save, async () => {
      const res = await api('PATCH', board.ctx.path(cur), changes);
      board.ctx.editing.delete(item.id);
      board.set(res);
      const again = board.cards.get(item.id);
      if (again) again.querySelector('button')?.focus();
    });
  });
  fill(card, form);
  title.focus();
}

/** "Add an item by hand" (``post(body)`` creates it and returns the new item). */
function addItemForm(board, post, { disabled = false, compact = false } = {}) {
  const kind = h('select', { 'aria-label': t('item.kind') },
    KINDS.map((k) => h('option', { value: k }, t('kind.' + k + '.one'))));
  const title = h('input', { class: 'title-in', placeholder: t('items.add_ph'), 'aria-label': t('item.title'), required: true, maxlength: 200 });
  const owner = h('input', { class: 'narrow', placeholder: t('item.owner'), 'aria-label': t('item.owner'), maxlength: 80 });
  const due = h('input', { class: 'narrow', placeholder: t('item.due_ph'), 'aria-label': t('item.due_label'), maxlength: 80 });
  const submit = h('button', { type: 'submit', class: 'btn', disabled, 'aria-label': t('items.add') },
    icon('plus'), h('span', { class: 'btn-label' }, t('items.add')));
  const form = h('form', { class: 'add', autocomplete: 'off' }, kind, title, owner, due, submit);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const body = { kind: kind.value, title: title.value.trim() };
    if (!body.title) return title.focus();
    if (owner.value.trim()) body.owner = owner.value.trim();
    if (due.value.trim()) body.due_text = due.value.trim();
    act(submit, async () => {
      board.set(await post(body));
      title.value = owner.value = due.value = '';
      title.focus();
    });
  });
  form.setDisabled = (off) => { submit.disabled = off; };
  if (compact) {  // a narrow pane: "who" and "when" show up once you start adding one
    form.classList.add('compact');
    form.addEventListener('focusin', () => form.classList.add('open'));
    form.addEventListener('focusout', (e) => {
      if (!form.contains(e.relatedTarget) && !title.value && !owner.value && !due.value) form.classList.remove('open');
    });
  }
  return form;
}

function sinkLabel(name) {
  const key = 'tool.' + name + '.name';
  return (STRINGS[LANG][key] || STRINGS.en[key]) ? t(key) : String(name || '');
}

// =========================================================================== Live

const LV = {};

const liveCtx = {
  id: 'live',
  live: true,
  items: S.live.items,
  editing: S.live.editing,
  expanded: S.live.expanded,
  showDismissed: false,
  sinks: () => S.live.sinks,
  readOnly: () => S.live.phase !== 'live' && S.live.phase !== 'ending',
  path: (item, action) => '/api/items/' + enc(item.id) + (action ? '/' + action : '')
    + (S.live.meeting ? '?meeting_id=' + enc(S.live.meeting.id) : ''),
  onRender: (item) => {
    if (LV.lines) markSaid(LV.lines, item);
    if (LV.voices) LV.voices.setItems(S.live.items.values());
  },
};

function buildLive() {
  const root = $('screen-live');
  homeNotices();  // in the meeting being replaced: kept, not emptied with it
  LV.root = root;
  LV.notices = h('div', { class: 'live-notices', id: 'live-notices' });

  LV.heroIcon = h('div', { class: 'hero-icon' });
  LV.heroTitle = h('h1', { id: 'live-heading', tabindex: '-1' });
  LV.heroText = h('p', { id: 'live-text' });
  LV.heroRow = h('div', { class: 'start-row', id: 'live-controls' });
  LV.modeLine = h('p', { class: 'mode-line', id: 'live-mode' });
  LV.queuedIdle = h('div', { class: 'queued', id: 'live-queued', role: 'status' });
  LV.idleMeterSlot = h('div', { class: 'row' });
  // the shortcut and its "Choose a shortcut": one recorder kept across redraws (a redraw mid-recording
  // must not forget it is recording)
  LV.shortcut = shortcutSetting({ prefix: 'live-shortcut', variant: 'live' });
  // between meetings: the state and what to do about it (by hand: Start meeting, the shortcut, the other
  // ways), then what came out of the meetings so far (the last one with its to-dos, the week in figures,
  // the ones before it)
  LV.lastCard = h('section', { class: 'card last-meeting', id: 'last-meeting', 'aria-labelledby': 'last-h' });
  LV.week = h('section', { class: 'card week', id: 'live-week', 'aria-labelledby': 'week-h' });
  LV.recent = h('section', { class: 'card recent', id: 'live-recent', 'aria-labelledby': 'recent-h' });
  LV.idle = h('div', { class: 'idle' },
    h('div', { class: 'hero' }, LV.heroIcon,
      h('div', { class: 'grow' }, LV.heroTitle, LV.heroText, LV.idleMeterSlot, LV.heroRow, LV.queuedIdle, LV.modeLine)),
    LV.week, LV.lastCard, LV.recent);

  LV.title = h('h1', { id: 'meeting-heading', tabindex: '-1' });
  LV.elapsed = h('span', { class: 'num' });
  LV.elapsedSep = h('span', { 'aria-hidden': 'true' }, '·');
  LV.subText = h('span');
  LV.pausedPill = h('span', { class: 'pill warn' }, icon('pause'), t('state.paused_short'));
  LV.meterSlot = h('div');
  LV.meters = h('div', { class: 'meters', role: 'group', 'aria-label': t('live.levels') });
  const sources = [...S.live.meters.keys()];
  S.live.meters.clear();
  for (const src of sources) meter(src);
  LV.extract = btn(t('live.extract_short'), { cls: 'small ghost', title: t('live.extract_help'), id: 'extract', onclick: (e) => extractNow(e.currentTarget) }, 'sparkle');
  LV.end = btn(t('live.end'), { cls: 'danger', id: 'end-meeting', onclick: (e) => endMeeting(e.currentTarget) }, 'stop');
  LV.ending = notice('info', 'clock', t('live.ending'));
  LV.queuedMeeting = h('div', { class: 'queued', id: 'live-queued', role: 'status' });  // one view on show at a time
  // a meeting started by hand: its clock, then Pause or Resume and Stop, where the eye lands first
  LV.clockTime = h('span', { class: 'num rec-time', role: 'timer', 'aria-label': t('live.clock_label') });
  LV.clock = h('div', { class: 'rec-clock', id: 'rec-clock' }, h('span', { class: 'rec-dot', 'aria-hidden': 'true' }), LV.clockTime);
  LV.controls = h('div', { class: 'live-actions', id: 'meeting-controls' });
  const head = LV.head = h('div', { class: 'live-head' },
    h('div', { class: 'title' }, LV.title,
      h('div', { class: 'sub' }, LV.elapsed, LV.elapsedSep, LV.subText, LV.pausedPill), LV.meterSlot),
    h('div', { class: 'live-side' }, LV.clock, LV.controls, LV.end));

  LV.transcript = h('div', { class: 'scroll transcript', id: 'transcript', role: 'log', 'aria-label': t('live.transcript'), tabindex: '0' });
  LV.tEmpty = h('p', { class: 'empty-line' }, t('live.nothing_yet'));
  LV.lines = h('div', { id: 'lines' });
  LV.partials = h('div', { id: 'partials', 'aria-hidden': 'true' });
  LV.transcript.append(LV.tEmpty, LV.lines, LV.partials);
  LV.jump = btn(t('live.jump'), { cls: 'small jump', hidden: true, onclick: () => { S.live.follow = true; stick(); LV.jump.hidden = true; } });
  LV.transcript.addEventListener('scroll', () => {
    const el = LV.transcript;
    S.live.follow = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (S.live.follow) LV.jump.hidden = true;
  });
  const transcriptPane = h('section', { class: 'card pane', 'aria-labelledby': 'tp-h' },
    h('div', { class: 'pane-head' }, h('h2', { id: 'tp-h' }, t('live.transcript'))), LV.transcript, LV.jump);

  LV.voices = new VoiceStrip('live');
  LV.board = new ItemBoard(liveCtx);
  LV.addForm = addItemForm(LV.board, (body) => api('POST', '/api/items', body), { compact: true });
  const dismissed = h('input', { type: 'checkbox', id: 'live-show-dismissed', checked: liveCtx.showDismissed });
  dismissed.addEventListener('change', () => LV.board.showDismissed(dismissed.checked));
  // "Update to-dos now" acts on the to-dos: it sits on their pane, not with the recording's controls
  const itemsPane = h('section', { class: 'card pane', 'aria-labelledby': 'ip-h' },
    h('div', { class: 'pane-head' }, h('h2', { id: 'ip-h' }, t('live.items')),
      h('div', { class: 'pane-tools' }, LV.extract, h('label', { for: 'live-show-dismissed' }, dismissed, t('items.show_dismissed')))),
    h('div', { class: 'scroll' }, LV.addForm, LV.board.el));

  LV.meeting = h('div', { class: 'live-meeting' }, head, LV.ending, LV.queuedMeeting, LV.voices.el, h('div', { class: 'panes' }, transcriptPane, itemsPane));
  fill(root);  // renderLive puts in the view on show: between meetings, or the meeting
  for (const [kind, data] of S.live.segs) {
    LV.lines.append(kind === 'seg' ? segLine(data) : screenLine(data));
    if (kind === 'seg') LV.voices.add(data, false);
  }
  for (const item of S.live.items.values()) markSaid(LV.lines, item);
  LV.voices.setItems(S.live.items.values());  // and its legend, once
  LV.tEmpty.hidden = S.live.segs.length > 0;
  renderPartials();
}

/** Only Live's view on show is in the page (the other keeps following the meeting meanwhile), and only
 *  while Live is the screen on show, not under the setup: a hidden title is not the page's title. Live's
 *  warnings lead between meetings; in a meeting they come under its controls (see placeNotices). */
function attachLive(inMeeting) {
  if (S.route.name !== 'live' || S.setup) return placeNotices();
  const view = inMeeting ? LV.meeting : LV.idle;
  // moved in only when it isn't there yet: placeNotices puts the shell's banners between the two, and
  // moving either on every render would take the focus off a banner's button and lose a click on it
  if (inMeeting && LV.notices.parentNode !== LV.meeting) LV.head.after(LV.notices);
  const want = inMeeting ? [view] : [LV.notices, view];
  if (LV.root.children.length !== want.length || want.some((el, i) => LV.root.children[i] !== el)) {
    fill(LV.root, ...want);
    if (inMeeting) stick();
  }
  placeNotices();
}

function inMeetingView() {
  return Boolean(S.live.meeting) && (S.live.phase === 'live' || S.live.phase === 'ending');
}

function renderLive() {
  if (!LV.root) buildLive();
  const inMeeting = inMeetingView();
  const view = inMeeting ? 'meeting' : 'idle';
  // a focus the change of view takes away (Stop pressed, the meeting over; Start, the meeting begun)
  const active = document.activeElement;
  const lost = !active || active === document.body
    || (LV.root.contains(active) && !(inMeeting ? LV.meeting : LV.idle).contains(active));
  attachLive(inMeeting);
  LV.root.classList.toggle('in-meeting', inMeeting);
  document.body.classList.toggle('live-meeting', inMeeting && S.route.name === 'live');
  LV.idle.hidden = inMeeting;
  LV.meeting.hidden = !inMeeting;
  (inMeeting ? LV.meterSlot : LV.idleMeterSlot).append(LV.meters);
  LV.meters.hidden = !S.live.meters.size || !['listening', 'paused', 'starting'].includes(stateKey());
  renderLiveNotices();
  if (inMeeting) renderLiveHead();
  else renderIdle();
  // the focus left on the title while Start was busy (the meeting wrapping up) goes to it once it can start
  const parked = LV.parked && document.activeElement === LV.heroTitle;
  if (((LV.view && LV.view !== view && lost) || parked) && S.route.name === 'live' && !S.setup) {
    const target = liveFocusTarget();
    if (target && target !== document.activeElement) focusLive(target);
    LV.parked = target === LV.heroTitle;
  }
  LV.view = view;
}

/** Where the keyboard focus goes on Live when meetings start by hand: Start between meetings (Return
 *  starts one), Resume while paused, the meeting's title while it records (never Stop: Return would end
 *  it). Listening all the time, nothing: the screen's title, as on every screen. */
function liveFocusTarget() {
  if (!LV.root || S.route.name !== 'live' || S.setup || !S.app || !isManual()) return null;
  if (!inMeetingView()) {
    const start = $('live-start');
    return start && !start.disabled ? start : LV.heroTitle;
  }
  const resume = stateKey() === 'paused' && $('live-pause');
  return resume || LV.title;
}

/** Put the focus on Live's control (see liveFocusTarget). Start put there by the app, not reached with
 *  Tab, takes Return but not Space: Live is a long page and Space is how one scrolls it, and a meeting
 *  must not start because someone wanted to read further down. */
let startParked = false;

function focusLive(target) {
  target.focus({ preventScroll: true });
  startParked = target.id === 'live-start' && document.activeElement === target;
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') startParked = false;  // moving through the controls: Start is then a button like any other
  if (e.key !== ' ' || !startParked || e.target.id !== 'live-start' || e.altKey || e.ctrlKey || e.metaKey) return;
  e.preventDefault();  // a button is pressed on Space's keyup only if its keydown went through
  window.scrollBy(0, (e.shiftKey ? -1 : 1) * innerHeight * 0.85);
}, true);
document.addEventListener('pointerdown', () => { startParked = false; }, true);
// a redraw that puts the focus back on a new Start (same id) keeps it parked; anywhere else, it is not
document.addEventListener('focusin', (e) => { if (e.target.id !== 'live-start') startParked = false; });

/** Something typed and not saved yet: a to-do being corrected or an agent's brief (their forms keep
 *  nothing once the screen is drawn again), text in an "Add" or key field, Settings changed. */
function workInProgress() {
  const main = $('main');
  if (main.querySelector('form.edit, form.prompt')) return true;
  if ([...main.querySelectorAll('form.add input, form.entry input')].some((el) => el.value.trim())) return true;
  return S.route.name === 'settings' && dirty();
}

/** The app was opened again (from Spotlight, the Dock, its menu): Live, with its Start control in
 *  focus, unless a dialog is open, the setup, or something is being typed: what you were doing there
 *  stays as it is (the window coming forward is enough). */
function focusStart() {
  if (S.setup || !S.app || $('ask').open || $('viewer').open || workInProgress()) return;
  if (S.route.name !== 'live') {
    S.focusStart = true;
    location.hash = '#/live';  // navigate() puts the focus on it
    return;
  }
  const target = liveFocusTarget() || $('primary');
  if (target) focusLive(target);
}

function renderLiveNotices() {
  if (!LV.notices || !S.app) return;
  const out = [];
  if (stateKey() === 'error' && S.app.error) {
    const fix = fixFor(S.app.error);
    out.push(notice('error', 'alert', h('span', {}, h('strong', {}, t('live.problem')), ' ', humanize(S.app.error)),
      fix && btn(fix.label, { cls: 'small', onclick: fix.run }),
      // started by hand, Try again is the screen's own control, beside Start or Stop
      !isManual() && btn(t('action.try_again'), { cls: 'small', id: 'try-again', onclick: (e) => meetingStart(e.currentTarget) })));
  }
  // two raw messages can say the same thing in plain words (one per model being downloaded…): shown once
  const seen = new Set([S.app.error ? humanize(S.app.error) : null]);
  const warnings = (S.app.warnings || []).filter((w) => {
    const text = humanize(w);
    if (S.dismissed.has(w) || seen.has(text)) return false;
    seen.add(text);
    return true;
  });
  for (const w of warnings.slice(0, 3)) {
    const fix = fixFor(w);
    out.push(notice('warn', 'alert', humanize(w),
      fix && btn(fix.label, { cls: 'small', onclick: fix.run }),
      h('button', { type: 'button', class: 'btn ghost small icon-only', 'aria-label': t('common.hide'), title: t('common.hide'),
        onclick: () => { S.dismissed.add(w); renderLiveNotices(); } }, icon('x'))));
  }
  if (warnings.length > 3) out.push(h('p', { class: 'small muted' }, t('live.more_warnings', { n: warnings.length - 3 })));
  fill(LV.notices, ...out);
  LV.notices.hidden = !out.length;
}

function renderLiveHead() {
  const m = S.live.meeting;
  const st = stateKey();
  const manual = isManual();
  LV.title.textContent = m.title || t('live.untitled');
  const mode = S.live.extraction === 'claude' ? t('live.mode_claude') : S.live.extraction ? t('live.mode_offline') : '';
  LV.subText.textContent = [t('live.started_at', { time: fmtClock(m.started_at) }), mode].filter(Boolean).join(' · ');
  const elapsed = meetingElapsed();
  LV.elapsed.textContent = elapsed != null ? fmtTs(elapsed) : '';
  // by hand, the clock stands beside Pause and Stop; listening all the time, it starts the line under the title
  LV.elapsed.hidden = LV.elapsedSep.hidden = manual;
  LV.clock.hidden = LV.controls.hidden = !manual;
  LV.end.hidden = manual;  // ending the meeting by hand is Stop
  LV.pausedPill.hidden = st !== 'paused';
  const live = S.live.phase === 'live' && ['listening', 'paused'].includes(st);
  LV.extract.disabled = LV.end.disabled = !live;
  LV.addForm.setDisabled(!live);
  LV.ending.hidden = S.live.phase !== 'ending';
  LV.tEmpty.textContent = t('live.nothing_yet');
  if (manual) renderMeetingControls(st, elapsed);
  renderQueued(LV.queuedMeeting);
}

/** A start pressed while the last meeting is wrapped up (the shortcut, the floating button, a link)
 *  waits for that wrap-up, then records (``start_queued``): said where the wrap-up is shown, with
 *  Cancel, which takes that start back as Stop does — no question, nothing of it is recorded yet.
 *  Stop now stays as it is: it cuts the wrap-up itself short. The box is always there, empty when no
 *  start waits, so a screen reader hears the line when it comes. */
function renderQueued(el) {
  const on = stateKey() === 'stopping' && Boolean(S.app && S.app.start_queued);
  const sig = on ? LANG : '';
  if (el.dataset.sig === sig) return;  // not under a press in progress
  el.dataset.sig = sig;
  const had = el.contains(document.activeElement);
  fill(el, on ? h('div', { class: 'notice info' }, icon('record'), h('div', { class: 'msg' }, t('live.m.queued')),
    btn(t('common.cancel'), { cls: 'small', id: 'live-queued-cancel', 'aria-label': t('live.m.queued_cancel_aria'),
      onclick: (e) => meetingAction('stop', e.currentTarget) })) : null);
  // taken back: the focus stays on this view (its title), not on the page's body
  if (had && !on && el.isConnected) (inMeetingView() ? LV.title : LV.heroTitle).focus({ preventScroll: true });
}

/** A meeting started by hand: its clock, then Pause (Resume) and Stop, Stop filled while it records. */
function renderMeetingControls(st, elapsed) {
  const wrapping = st === 'stopping' || S.live.phase === 'ending';
  LV.clock.className = 'rec-clock ' + (wrapping ? 'stopping' : st);
  if (!wrapping) LV.clockTime.textContent = elapsed != null ? fmtTs(elapsed) : '';
  const parts = [];
  if (wrapping) {
    if (st === 'stopping') parts.push(btn(t('action.stop_now'), { cls: 'danger', id: 'live-stop-now', onclick: (e) => stopNow(e.currentTarget) }));
  } else if (st === 'error') {
    parts.push(btn(t('action.stop'), { id: 'live-stop', onclick: (e) => meetingAction('stop', e.currentTarget) }, 'stop'),
      btn(t('action.try_again'), { cls: 'primary', id: 'live-retry', onclick: (e) => meetingStart(e.currentTarget) }, 'refresh'));
  } else {
    const paused = st === 'paused';
    parts.push(paused
      ? btn(t('action.resume'), { cls: 'primary', id: 'live-pause', onclick: (e) => listen('resume', e.currentTarget) }, 'play')
      : btn(t('action.pause'), { id: 'live-pause', disabled: st !== 'listening', onclick: (e) => listen('pause', e.currentTarget) }, 'pause'),
    btn(t('action.stop'), { cls: paused ? '' : 'primary', id: 'live-stop', onclick: (e) => meetingAction('stop', e.currentTarget) }, 'stop'));
  }
  const sig = [LANG, st, wrapping].join('|');  // as Start's: not under a press in progress
  if (sig !== LV.controlsSig || !LV.controls.childElementCount) {
    LV.controlsSig = sig;
    keepFocus(LV.controls, () => fill(LV.controls, ...parts));
  }
}

function renderIdle() {
  const st = stateKey();
  if (isManual()) renderStartPanel(st);
  else renderListeningHero(st);
  renderLastMeeting();
}

/** Between meetings, when they start by hand: one large Start meeting (in focus, so Return starts it),
 *  the keyboard shortcut with "Choose a shortcut", and the line that says how meetings start. */
function renderStartPanel(st) {
  const views = {
    stopped: ['', t('live.m.stopped'), t('live.m.stopped_text')],
    starting: ['', t('live.m.starting'), t('live.idle.starting_text')],
    listening: ['listening', t('live.m.recording'), t('live.m.recording_text')],
    paused: ['paused', t('live.idle.paused'), t('live.m.paused_text')],
    // "the next one can start in a moment", unless one waits for it already (renderQueued says so)
    stopping: ['', t('live.m.stopping'), S.app.start_queued ? t('live.m.stopping_queued_text') : t('live.m.stopping_text')],
    error: ['error', t('live.idle.error'), t('live.m.error_text')],
  };
  const [cls, title, text] = views[st];
  LV.heroIcon.className = 'hero-icon ' + cls;
  fill(LV.heroIcon);
  LV.heroTitle.textContent = title;
  LV.heroText.textContent = text;
  const huge = 'primary huge';
  const parts = [];
  if (st === 'stopped') {
    parts.push(btn(t('action.start_meeting'), { cls: huge, id: 'live-start', onclick: (e) => meetingStart(e.currentTarget) }, 'record'));
  } else if (st === 'error') {
    parts.push(btn(t('action.try_again'), { cls: huge, id: 'live-start', onclick: (e) => meetingStart(e.currentTarget) }, 'refresh'),
      S.app.error && btn(t('action.dismiss'), { cls: 'ghost', id: 'live-dismiss', onclick: (e) => meetingAction('stop', e.currentTarget) }));
  } else if (st === 'starting' || st === 'stopping') {
    // a start can wait minutes for the speech model, then records by itself: it is cancelled here, where
    // it was asked for (no question: nothing has been recorded yet)
    parts.push(h('button', { type: 'button', class: 'btn ' + huge, id: 'live-start', disabled: true }, spinner(), stateText()),
      st === 'starting' && btn(t('action.stop'), { id: 'live-stop', onclick: (e) => meetingAction('stop', e.currentTarget) }, 'stop'),
      st === 'stopping' && btn(t('action.stop_now'), { cls: 'danger small', id: 'live-stop-now', onclick: (e) => stopNow(e.currentTarget) }));
  } else {  // recording, its page on its way
    const paused = st === 'paused';
    parts.push(btn(paused ? t('action.resume') : t('action.pause'), { cls: paused ? 'primary' : '', id: 'live-pause',
      onclick: (e) => listen(paused ? 'resume' : 'pause', e.currentTarget) }, paused ? 'play' : 'pause'),
    btn(t('action.stop'), { cls: paused ? '' : 'primary', id: 'live-stop', onclick: (e) => meetingAction('stop', e.currentTarget) }, 'stop'));
  }
  const between = st === 'stopped' || st === 'error';
  if (between) LV.shortcut.draw();
  // drawn again only when what it shows changes: a state event between the press of a button and its
  // release would otherwise put a new button under the pointer, and the click would be lost
  const sig = [LANG, st, Boolean(S.app.error), between && LV.shortcut.childElementCount > 0].join('|');
  if (sig !== LV.heroSig || !LV.heroRow.childElementCount) {
    LV.heroSig = sig;
    keepFocus(LV.heroRow, () => fill(LV.heroRow, ...parts, between && LV.shortcut.childElementCount ? LV.shortcut : null));
  }
  LV.heroRow.hidden = false;
  renderQueued(LV.queuedIdle);
  if (between) setModeLine(t('live.mode.manual', { where: fmtList(startPlaces()) }));
  LV.modeLine.hidden = !between;
}

/** How meetings start, in a sentence, and "Change" (the same sentence is not drawn again: see heroSig). */
function setModeLine(text) {
  if (LV.modeLine.dataset.text === text && LV.modeLine.childElementCount) return;
  LV.modeLine.dataset.text = text;
  fill(LV.modeLine, text, ' ', h('a', { href: '#/settings/general/starting', id: 'live-mode-change', 'aria-label': t('live.mode.change_aria') },
    t('live.mode.change')));
}

/** Listening all the time (automatic): the state and what it means, as before, and that meetings
 *  start by themselves here, with the way to change it. Start and Resume are the recorder's. */
function renderListeningHero(st) {
  const minutes = S.settings ? S.settings.values.always_on.idle_split_minutes : 10;
  const views = {
    listening: ['mic', 'listening', t('live.idle.listening'), minutes > 0 ? t('live.idle.listening_text', { n: fmtNum(minutes) }) : t('live.idle.listening_text_nosplit')],
    paused: ['pause', 'paused', t('live.idle.paused'), t('live.idle.paused_text')],
    starting: ['mic', '', t('live.idle.starting'), t('live.idle.starting_text')],
    stopping: ['clock', '', t('live.idle.stopping'), t('live.idle.stopping_text')],
    error: ['alert', 'error', t('live.idle.error'), t('live.idle.error_text')],
    stopped: ['micOff', '', t('live.idle.stopped'), t('live.idle.stopped_text')],
  };
  const [ic, cls, title, text] = views[st];
  LV.heroIcon.className = 'hero-icon ' + cls;
  fill(LV.heroIcon, icon(ic, 'xl'));
  LV.heroTitle.textContent = title;
  LV.heroText.textContent = text;
  if (st === 'stopped') LV.heroText.append(shortcutHint() || '');
  fill(LV.heroRow);
  LV.heroSig = null;
  LV.heroRow.hidden = true;
  setModeLine(t('live.mode.automatic'));
  LV.modeLine.hidden = false;
}

/** The meetings, newest first, as far back as the last 7 days go at least: the week's figures count
 *  every one of them, and always-on makes a meeting of each talk after a quiet spell. */
async function recentMeetings() {
  const since = Date.now() - 7 * 86400000;
  const page = (offset) => api('GET', '/api/meetings?limit=100&offset=' + offset);
  const first = await page(0);
  const rows = first.meetings, seen = new Set(rows.map((r) => r.folder));
  let total = first.total;
  while (rows.length && rows.length < total && (toDate(rows[rows.length - 1].started_at) || 0) >= since) {
    const more = await page(rows.length);
    const fresh = more.meetings.filter((r) => !seen.has(r.folder));  // one that began meanwhile moves the rest down
    if (!fresh.length) break;
    for (const r of fresh) { seen.add(r.folder); rows.push(r); }
    total = more.total;
  }
  return rows;
}

const refreshLast = debounce(async () => {
  S.lastMeetingFailed = false;
  try {
    S.recent = await recentMeetings();
    // not one still recording: between meetings that is the one wrapping up (a meeting started by hand
    // and stopped with nothing said has no folder once it is over), and the next refresh brings it
    const m = S.lastMeeting = S.recent.find((r) => !r.live) || null;
    S.lastMeetingLoaded = true;
    // the last meeting itself: its to-dos and decisions, not only how many, and who spoke when
    const detail = m ? (await api('GET', '/api/meetings/' + enc(m.folder))).meeting : null;
    S.lastItems = detail ? Object.values(detail.items || {}) : [];
    S.lastSegments = detail ? detail.segments || [] : [];
    if (LV.root && !inMeetingView()) renderLastMeeting();
  } catch (_) {  // shown as "no meetings yet", and asked again on the next meeting event
    S.lastMeetingFailed = true;
    if (LV.root && !inMeetingView()) renderLastMeeting();
  }
}, 300);

/** A person as themselves: their initial on a pale tint of their own hue (the same name, the same hue). */
function person(name, mine = false) {
  const label = mine ? t('item.you') : String(name || '');
  let hash = 0;
  for (const ch of String(mine ? S.userName : name).toLowerCase()) hash = (hash * 31 + ch.codePointAt(0)) % 360;
  const disc = h('span', { class: 'initial', 'aria-hidden': 'true' }, (String(mine ? S.userName : name).trim()[0] || '?').toUpperCase());
  disc.style.setProperty('--h', String(hash));
  return h('span', { class: 'who' + (mine ? ' you' : '') }, disc, label);
}

function renderLastMeeting() {
  if (!S.lastMeetingLoaded && !S.lastMeetingFailed) { refreshLast(); }
  renderWeek();
  const m = S.lastMeeting;
  const just = S.live.lastEnded && m && S.live.lastEnded.id === m.id;
  if (!m && !S.lastMeetingLoaded && !S.lastMeetingFailed) {  // the panels keep their place while the meetings load
    fill(LV.lastCard, h('h2', { id: 'last-h' }, t('live.last_meeting')), h('p', { class: 'muted' }, t('common.loading')));
    fill(LV.recent, h('h2', { id: 'recent-h' }, t('live.earlier')), h('p', { class: 'muted' }, t('common.loading')));
    return;
  }
  if (!m) {
    fill(LV.lastCard, h('h2', { id: 'last-h' }, t('live.no_meetings')), h('p', { class: 'muted' }, t('live.no_meetings_text')));
    fill(LV.recent, h('h2', { id: 'recent-h' }, t('live.earlier')), h('p', { class: 'empty-slot' }, t('live.none_earlier')));
    return;
  }
  const items = (S.lastItems || []).filter((i) => !i.dismissed && (i.kind === 'action' || i.kind === 'decision'))
    .sort((a, b) => (a.kind === b.kind ? 0 : a.kind === 'action' ? -1 : 1));
  const shown = items.slice(0, 6);
  const rows = shown.map((i) => h('li', { class: 'kept-row ' + i.kind + (i.status === 'done' ? ' done' : '') },
    h('i', { class: 'key mark ' + i.kind, title: t('kind.' + i.kind + '.one') }),
    h('span', { class: 'title' }, i.title),
    h('span', { class: 'kept-meta' }, i.owner && person(i.owner, isMine(i.owner)), fmtDue(i) && h('span', { class: 'due' }, fmtDue(i)))));
  fill(LV.lastCard,
    h('div', { class: 'panel-head' },
      h('div', { class: 'grow' }, h('h2', { id: 'last-h' }, m.title || t('meetings.untitled')),
        h('p', { class: 'meta-line' }, [just ? t('live.just_ended') : t('live.last_meeting'), fmtDay(m.started_at), fmtClock(m.started_at),
          m.duration_s ? fmtDuration(m.duration_s) : ''].filter(Boolean).join(' · '))),
      h('a', { class: 'btn', href: '#/meetings/' + enc(m.folder) }, m.has_notes ? t('live.open_notes') : t('common.open'))),
    m.tldr && h('p', { class: 'tldr' }, m.tldr),
    lastVoices(m),
    items.length ? h('ul', { class: 'kept-list', 'aria-label': t('live.items') }, rows) : h('p', { class: 'empty-slot' }, t('live.last_items_none')),
    items.length > shown.length && h('p', { class: 'small muted' }, t('live.more_items', { n: fmtNum(items.length - shown.length) })),
    h('div', { class: 'row panel-foot' }, meetingChips(m), meetingSent(m)));
  const earlier = (S.recent || []).filter((r) => r !== m && !r.live).slice(0, 6);
  fill(LV.recent,
    h('div', { class: 'panel-head' }, h('h2', { id: 'recent-h', class: 'grow' }, t('live.earlier')),
      h('a', { href: '#/meetings', class: 'view-all' }, t('meetings.all'))),
    earlier.length ? h('ul', { class: 'recent-list' }, earlier.map((r) => h('li', {},
      h('a', { href: '#/meetings/' + enc(r.folder) },
        h('span', { class: 'title' }, r.title || t('meetings.untitled')),
        h('span', { class: 'when' }, [['today', 'yesterday'].includes(dayBucket(r.started_at)) ? t('meetings.group.' + dayBucket(r.started_at))
          : fmtDay(r.started_at), fmtClock(r.started_at)].join(' · ')),
        h('span', { class: 'count num' }, r.counts && r.counts.action ? tp('count.actions', r.counts.action) : '—')))))
      : h('p', { class: 'empty-slot' }, t('live.none_earlier')));
}

/** The last meeting's voice strip, without its own frame: it sits inside the meeting's panel. */
function lastVoices(m) {
  const segs = S.lastSegments || [];
  if (!segs.length) return null;
  const strip = new VoiceStrip('last');
  for (const seg of segs) strip.add(seg, false);
  strip.setItems(S.lastItems || []);  // and its legend, once
  strip.setTotal(m.duration_s || 0);
  strip.el.classList.add('inline');
  return strip.el;
}

/** The last 7 days in four figures, counted from the meetings themselves. */
function renderWeek() {
  const since = Date.now() - 7 * 86400000;
  const rows = (S.recent || []).filter((r) => (toDate(r.started_at) || 0) >= since);
  const sum = (f) => rows.reduce((n, r) => n + f(r), 0);
  const figures = [
    ['meetings', rows.length],
    ['actions', sum((r) => (r.counts && r.counts.action) || 0)],
    ['mine', sum((r) => r.my_actions || 0)],
    ['decisions', sum((r) => (r.counts && r.counts.decision) || 0)],
  ];
  fill(LV.week, h('h2', { id: 'week-h' }, t('live.week')),
    h('dl', { class: 'figures' }, figures.map(([key, n]) => h('div', { class: key },
      h('dt', {}, t('live.week.' + key)), h('dd', { class: 'num' }, S.lastMeetingLoaded ? fmtNum(n) : '—')))));
}

// -------------------------------------------------------------- live meeting events

function applyLiveState(st) {
  const L = S.live;
  if (st.user_name) setUserName(st.user_name);
  L.sinks = st.sinks || [];
  L.extraction = st.extraction;
  setMeters(st.sources || []);
  if (st.cost) setCost(st.cost);
  if (st.meeting) {
    const m = st.meeting;
    if (!L.meeting || L.meeting.id !== m.id) startMeeting({ id: m.id, title: m.title, started_at: m.started_at, folder: basename(st.meeting_dir) });
    else if (L.phase !== 'ending') L.phase = 'live';
    L.meeting.title = m.title || L.meeting.title;
    const timeline = [
      ...(m.segments || []).map((s) => [s.t_start, () => addSegment(s)]),
      ...(m.screen_notes || []).map((n) => [n.t, () => addScreen(n)]),
    ].sort((a, b) => a[0] - b[0]);
    for (const [, fn] of timeline) fn();
    for (const item of Object.values(m.items || {})) liveBoard().set(item);
  } else if (L.meeting && (L.phase === 'live' || L.phase === 'ending')) {
    L.phase = 'ended';  // it ended while we were disconnected
  }
  liveBoard().renderAll();  // the tools may have changed
  if (S.route.name === 'live') renderLive();
}

function liveBoard() {
  if (!LV.root) buildLive();
  return LV.board;
}

function onAssistantStarted(d) {
  S.live.extraction = d.extraction;
  setMeters(d.sources || [], true);
  api('GET', '/api/state').then(applyLiveState).catch(() => { /* stopped again already */ });
}

function onAssistantStopped(d) {
  if (d.cost) setCost(d.cost);
  // after a stop or restart the previous assistant may finish an ended meeting's agents for minutes:
  // its "stopped" comes while listening again, and the meters are the new one's
  if (['listening', 'paused', 'starting'].includes(stateKey())) return;
  setMeters([], true);
  if (S.route.name === 'live') renderLive();
}

function startMeeting(m) {
  const L = S.live;
  L.meeting = { id: m.id, title: m.title || null, started_at: m.started_at, folder: m.folder || null };
  L.phase = 'live';
  L.lastEnded = null;
  L.segs = [];
  for (const set of [L.seen, L.partials, L.editing, L.expanded]) set.clear();
  liveBoard().clear();
  LV.voices.clear();
  fill(LV.lines);
  fill(LV.partials);
  LV.tEmpty.hidden = false;
  L.follow = true;
  LV.jump.hidden = true;
}

function liveEvent(type, d, ev) {
  const L = S.live;
  const otherMeeting = d.meeting_id !== undefined && (!L.meeting || d.meeting_id !== L.meeting.id);
  if (otherMeeting && type !== 'meeting.started') return;  // e.g. late agent results of an older meeting
  switch (type) {
    case 'meeting.started':
      if (L.meeting && L.meeting.id === d.meeting_id && L.phase === 'live') return;
      startMeeting({ id: d.meeting_id, title: d.title, started_at: ev.ts, folder: basename(d.dir) });
      refreshMeetingsSoon();
      break;
    case 'meeting.updated':
      if (d.title) L.meeting.title = d.title;
      break;
    case 'meeting.ending':
      L.phase = 'ending';
      break;
    case 'meeting.ended': {
      L.phase = 'ended';
      if (!d.dir && !d.notes) {  // nothing was said: nothing kept, no notes to open
        toast(t('toast.meeting_empty'), 'info', d.title || L.meeting.title || t('toast.meeting_over'));  // not "in progress": it is over
        S.lastMeetingLoaded = false;
        refreshLast();
        break;
      }
      const folder = basename(d.dir) || L.meeting.folder;
      L.lastEnded = { id: d.meeting_id, title: d.title || L.meeting.title, folder, items: d.items || 0 };
      toast(tp('toast.meeting_ended', d.items || 0), 'ok', d.title || L.meeting.title || t('meetings.untitled'),
        folder ? { label: t('live.open_notes'), run: () => go('#/meetings/' + enc(folder)) } : null);
      S.lastMeetingLoaded = false;
      refreshLast();
      refreshMeetingsSoon();
      break;
    }
    case 'meeting.notes_updated':
      if (d.folder === undefined && d.notes) toast(t('toast.notes_updated_agents'), 'ok', t('toast.notes_updated'));
      return;
    case 'segment':
      return addSegment(d.segment);
    case 'screen':
      return addScreen(d.note);
    case 'transcript.partial':
      if (d.text) L.partials.set(d.source, { speaker: d.speaker, text: d.text, at: Date.now() });
      else L.partials.delete(d.source);
      return renderPartials();
    case 'level':
      return setLevel(d);
    case 'extraction':
      if (d.mode) L.extraction = d.mode === 'llm' ? 'claude' : d.mode;
      break;
    case 'item.added':
    case 'item.updated':
    case 'agent.updated':
      return liveBoard().set(d.item);
    case 'item.pushed': {
      const item = L.items.get(d.item_id);
      if (!item) return;
      item.pushed = { ...item.pushed, [d.sink]: d.ref || '' };
      return liveBoard().render(item);
    }
    case 'notes.pushed':
      return toast(sinkLabel(d.sink), 'ok', t('toast.notes_pushed'), d.ref ? { label: t('common.open'), href: d.ref } : null);
    case 'agent.budget':
      return toast(t('toast.agent_budget', { id: d.item_id }), 'warn', t('toast.agent_budget_title'));
    case 'agent.held':
      return toast(t('toast.agent_held', { id: d.item_id }), 'warn', t('toast.agent_held_title'));
    default:
      return;
  }
  if (S.route.name === 'live') renderLive();
}

// ---------------------------------------------------------------------- transcript

function line(cls, ts, title, speaker, text, segId) {
  return h('div', { class: 'seg ' + cls, dataset: segId ? { seg: segId } : null },
    h('span', { class: 'ts', title }, ts),
    h('div', { class: 'body' }, h('span', { class: 'spk' }, speaker), h('span', { class: 'txt' }, text)));
}

/** The words a to-do or a decision came from, marked in the transcript ``box`` with what they became. */
function markSaid(box, item) {
  for (const old of box.querySelectorAll(`.said-tag[data-item="${CSS.escape(item.id)}"]`)) {
    const lineEl = old.closest('[data-seg]');
    old.remove();
    if (lineEl && !lineEl.querySelector('.said-tag')) lineEl.classList.remove('said');
  }
  if (item.dismissed || !['action', 'decision'].includes(item.kind)) return;
  for (const id of item.segment_ids || []) {
    const lineEl = box.querySelector(`[data-seg="${CSS.escape(id)}"]`);
    if (!lineEl) continue;
    lineEl.classList.add('said');
    lineEl.querySelector('.body').append(h('span', { class: 'said-tag ' + item.kind, dataset: { item: item.id }, title: item.title },
      t('kind.' + item.kind + '.one')));
  }
}

function speakerOf(seg) {
  const mine = seg.source === 'mic';
  // the default labels (capture.mic.label = "Me"…) follow the app's language; a name you set is kept
  if (!seg.speaker || seg.speaker === (mine ? 'Me' : 'Others')) return mine ? t('live.me') : t('live.others');
  return seg.speaker;
}

function segLine(seg) {
  return line(seg.source === 'mic' ? 'me' : 'others', fmtTs(seg.t_start), fmtClock(seg.wall_time), speakerOf(seg), seg.text, seg.id);
}

function screenLine(note) {
  return line('screen', fmtTs(note.t), fmtClock(note.wall_time), t('live.screen'), note.description);
}

function addSegment(seg) {
  if (!seg || S.live.seen.has(seg.id)) return;
  liveBoard();
  S.live.seen.add(seg.id);
  S.live.segs.push(['seg', seg]);
  if (S.live.partials.delete(seg.source)) renderPartials();
  appendLine(segLine(seg));
  LV.voices.add(seg);
}

function addScreen(note) {
  if (!note || S.live.seen.has(note.id)) return;
  liveBoard();
  S.live.seen.add(note.id);
  S.live.segs.push(['screen', note]);
  appendLine(screenLine(note));
}

function appendLine(el) {
  LV.lines.append(el);
  LV.tEmpty.hidden = true;
  stick();
}

function stick() {
  if (!LV.transcript) return;
  if (S.live.follow) LV.transcript.scrollTop = LV.transcript.scrollHeight;
  else LV.jump.hidden = false;
}

function renderPartials() {
  if (!LV.partials) return;
  fill(LV.partials, ...[...S.live.partials].map(([src, p]) =>
    line('partial ' + (src === 'mic' ? 'me' : 'others'), '', '', speakerOf({ source: src, speaker: p.speaker }), p.text + '…')));
  stick();
}

// ------------------------------------------------------------------- who spoke when

const AXIS_STEPS = [60, 120, 300, 600, 900, 1200, 1800, 3600, 7200];

/** The axis under the strip: 0, then one step all the way (3 to 7 labels). */
function axisStep(total) { return AXIS_STEPS.find((s) => total / s <= 6) || AXIS_STEPS[AXIS_STEPS.length - 1]; }

/**
 * A meeting as the two voices it is made of: what you said (your microphone) and what the others
 * said (the meeting audio), each line drawn at its time and for its length, with the to-dos and
 * decisions on a lane above, at the moment they were said. The legend is the share of the talk.
 * Positions are fractions of ``--total`` (CSS), so a live meeting's strip only updates that.
 */
class VoiceStrip {
  constructor(id) {
    this.id = id;
    this.at = new Map();  // segment id -> its start, for the items' marks
    this.talk = { me: 0, others: 0 };
    this.end = 0;
    this.total = 0;
    this.step = 0;
    this.counts = { action: 0, decision: 0 };
    this.tracks = {};
    const lane = (name, label) => {
      const track = h('div', { class: 'track' });
      this.tracks[name] = track;
      return h('div', { class: 'lane ' + name }, h('span', { class: 'lane-name' }, label), track);
    };
    this.legend = h('div', { class: 'legend', id: id + '-voices-legend' });
    this.axis = h('div', { class: 'track axis' });
    this.plot = h('div', { class: 'voices-plot', role: 'img' },
      lane('kept', t('voices.kept')), lane('me', t('live.me')), lane('others', t('live.others')),
      h('div', { class: 'lane axis-lane', 'aria-hidden': 'true' }, h('span', { class: 'lane-name' }), this.axis));
    this.el = h('section', { class: 'voices', 'aria-labelledby': id + '-voices-h' },
      h('div', { class: 'voices-head' }, h('h2', { id: id + '-voices-h' }, t('voices.title')), this.legend), this.plot);
    this.setTotal(0);
  }

  clear() {
    this.at.clear();
    this.talk = { me: 0, others: 0 };
    this.end = 0;
    for (const track of Object.values(this.tracks)) fill(track);
    this.setTotal(0);
    this.setItems([]);
  }

  /** One more line; ``legend`` false when more follow at once (the legend is drawn after the last). */
  add(seg, legend = true) {
    if (!seg || this.at.has(seg.id)) return;
    const lane = seg.source === 'mic' ? 'me' : 'others';
    const dur = Math.max(0, (seg.t_end || 0) - (seg.t_start || 0));
    this.at.set(seg.id, seg.t_start || 0);
    this.talk[lane] += dur;
    this.end = Math.max(this.end, seg.t_end || 0);
    const bar = h('i', { title: fmtTs(seg.t_start) + ' · ' + speakerOf(seg) });
    bar.style.setProperty('--s', String(seg.t_start || 0));
    bar.style.setProperty('--d', String(dur));
    this.tracks[lane].append(bar);
    if (this.end > this.total) this.setTotal(this.end);
    if (legend) this.renderLegend();
  }

  /** Where each to-do and decision was said: its first line's time. */
  setItems(items) {
    const marks = [];
    this.counts = { action: 0, decision: 0 };
    for (const item of items) {
      if (item.dismissed || !(item.kind in this.counts)) continue;
      const times = (item.segment_ids || []).map((id) => this.at.get(id)).filter((x) => x != null);
      if (!times.length) continue;
      const at = Math.min(...times);
      this.counts[item.kind]++;
      const mark = h('i', { class: 'mark ' + item.kind, title: t('kind.' + item.kind + '.one') + ' · ' + fmtTs(at) + ' · ' + item.title });
      mark.style.setProperty('--s', String(at));
      marks.push(mark);
    }
    fill(this.tracks.kept, ...marks);
    this.renderLegend();
  }

  /** The strip's length in seconds: the meeting so far (live, a whole minute), or up to its last words. */
  setTotal(seconds) {
    const total = Math.max(60, this.end, seconds || 0);
    if (Math.abs(total - this.total) < 0.5) return;
    this.total = total;
    this.plot.style.setProperty('--total', String(total));
    const step = axisStep(total), count = Math.floor(total / step);
    if (step !== this.step || count !== this.count) {
      this.step = step;
      this.count = count;
      const labels = [];
      for (let s = 0; s <= total; s += step) {
        // every other one, from four steps up, is left out on a phone (app.css): 3 or 4 labels there
        const label = h('span', { class: count >= 4 && (s / step) % 2 === 1 ? 'minor' : null }, s === 0 ? '0' : fmtDuration(s));
        label.style.setProperty('--s', String(s));
        labels.push(label);
      }
      fill(this.axis, ...labels);
    }
    // the label at the right end ends on its tick, inside the strip
    const last = this.axis.lastElementChild;
    if (last && last !== this.axis.firstElementChild) last.classList.toggle('end', (total - this.count * step) / total < 0.06);
  }

  renderLegend() {
    const talked = this.talk.me + this.talk.others;
    const me = talked ? Math.round((this.talk.me / talked) * 100) : 0;
    const parts = [];
    if (talked) {  // the two shares add up to 100: the second is what the first leaves
      parts.push(h('span', {}, h('i', { class: 'key me' }), t('voices.share', { who: t('live.me'), pct: fmtNum(me) })));
      parts.push(h('span', {}, h('i', { class: 'key others' }), t('voices.share', { who: t('live.others'), pct: fmtNum(100 - me) })));
    }
    if (this.counts.action) parts.push(h('span', {}, h('i', { class: 'key mark action' }), tp('count.actions', this.counts.action)));
    if (this.counts.decision) parts.push(h('span', {}, h('i', { class: 'key mark decision' }), tp('count.decisions', this.counts.decision)));
    fill(this.legend, ...parts);
    this.plot.setAttribute('aria-label', talked
      ? [t('voices.share', { who: t('live.me'), pct: fmtNum(me) }), t('voices.share', { who: t('live.others'), pct: fmtNum(100 - me) }),
        this.counts.action ? tp('count.actions', this.counts.action) : '', this.counts.decision ? tp('count.decisions', this.counts.decision) : '']
        .filter(Boolean).join(', ')
      : t('live.nothing_yet'));
  }
}

// --------------------------------------------------------------------- meters, cost

function setMeters(sources, reset = false) {
  if (reset) {
    for (const m of S.live.meters.values()) m.el.remove();
    S.live.meters.clear();
  }
  S.live.sources = sources;
  for (const desc of sources) {
    const name = String(desc).split(':')[0].trim();
    if (name && name !== 'transcript import') meter(name, String(desc));
  }
  if (LV.meters) LV.meters.hidden = !S.live.meters.size || !['listening', 'paused', 'starting'].includes(stateKey());
}

function meter(name, desc) {
  let m = S.live.meters.get(name);
  if (!m) {
    if (!LV.meters) buildLive();
    const bar = h('span', { class: 'fill' });
    const label = STRINGS.en['src.' + name] ? t('src.' + name) : name;
    const el = h('div', { class: 'meter', title: desc || label }, h('span', { class: 'dot' }), h('span', {}, label), h('span', { class: 'bar' }, bar));
    LV.meters.append(el);
    m = { el, fill: bar, at: 0 };
    S.live.meters.set(name, m);
  } else if (desc) {
    m.el.title = desc;
  }
  return m;
}

function setLevel(d) {
  const m = meter(d.source);
  const db = typeof d.db === 'number' ? d.db : -90;  // -inf arrives as null
  m.fill.style.transform = 'scaleX(' + Math.max(0, Math.min(1, (db + 60) / 60)).toFixed(2) + ')';
  m.el.classList.toggle('speech', Boolean(d.speech));
  m.at = Date.now();
  if (LV.meters && LV.meters.hidden && ['listening', 'paused', 'starting'].includes(stateKey())) LV.meters.hidden = false;
}

function setCost(c) {
  if (!c || !c.today) return;
  S.live.cost = c;
  const el = $('spend');
  if (el) fillSpend(el);
}

// ------------------------------------------------------------------ live controls

async function extractNow(button) {
  await act(button, async () => {
    await api('POST', '/api/extract');
    toast(t('toast.extracted'), 'ok');
  });
  renderLiveHead();
}

async function endMeeting(button) {
  const ok = await ask({ title: t('ask.end_title'), body: t('ask.end_body'), ok: t('live.end') });
  if (!ok) return;
  await act(button, async () => {
    await api('POST', '/api/meeting/end');
    // meeting.ending / meeting.ended may already have arrived on the event stream
    if (S.live.phase === 'live') S.live.phase = 'ending';
  });
  renderLive();
}

// ====================================================================== markdown

const MD_HEADING = /^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const MD_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const MD_CHECK = /^\[([ xX])\]\s+/;
const MD_RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const MD_FENCE = /^\s*(```|~~~)/;
const MD_INLINE = new RegExp([
  '`([^`\\n]+)`',                                   // 1 code
  '\\[([^\\]\\n]+)\\]\\(([^()\\s]+)\\)',            // 2, 3 link
  '\\*\\*(\\S(?:.*?\\S)?)\\*\\*',                   // 4 bold
  '__(\\S(?:.*?\\S)?)__',                           // 5 bold
  '~~(\\S(?:.*?\\S)?)~~',                           // 6 strike
  '(?<![\\w*])\\*(\\S(?:.*?\\S)?)\\*(?![\\w*])',    // 7 italic
  '(?<!\\w)_(\\S(?:.*?\\S)?)_(?!\\w)',              // 8 italic
  '(https?:\\/\\/[^\\s<>"\'`)\\]]+[^\\s<>"\'`)\\].,;:!?])', // 9 bare link
].join('|'), 'g');

/**
 * Markdown → a DocumentFragment, for the notes and e-mails (the same subset as voicestream/render.py:
 * headings, nested lists, task boxes, quotes, fenced code, rules, bold/italic/strike, code, links).
 * Every piece of text becomes a text node; the only elements are the fixed ones created here, and a
 * link keeps its target only for http(s) and mailto URLs — so no input can inject markup or script.
 */
function renderMarkdown(md) {
  const out = document.createDocumentFragment();
  let para = [], quote = [], fence = null;
  let lists = [];  // open lists: {indent, tag, el, li}

  const flush = () => {
    if (para.length) out.append(lines(h('p'), para));
    if (quote.length) out.append(h('blockquote', {}, lines(h('p'), quote)));
    para = []; quote = []; lists = [];
  };
  const lines = (el, rows) => { rows.forEach((row, i) => { if (i) el.append(h('br')); inline(el, row); }); return el; };

  for (const raw of String(md || '').replace(/\r\n?/g, '\n').split('\n')) {
    if (fence) {
      if (MD_FENCE.test(raw)) { out.append(h('pre', {}, h('code', {}, fence.join('\n')))); fence = null; }
      else fence.push(raw);
      continue;
    }
    let m;
    if (MD_FENCE.test(raw)) { flush(); fence = []; }
    else if (!raw.trim()) flush();
    else if ((m = raw.match(MD_HEADING))) { flush(); out.append(inline(h('h' + m[1].length), m[2])); }
    else if (MD_RULE.test(raw)) { flush(); out.append(h('hr')); }
    else if (raw.trimStart().startsWith('>')) {
      if (!quote.length) flush();
      quote.push(raw.trimStart().slice(1).trim());
    } else if ((m = raw.match(MD_ITEM))) {
      if (!lists.length) flush();
      const indent = m[1].replace(/\t/g, '    ').length;
      const tag = /\d/.test(m[2][0]) ? 'ol' : 'ul';
      let text = m[3];
      const box = text.match(MD_CHECK);
      while (lists.length && (indent < lists[lists.length - 1].indent
        || (indent === lists[lists.length - 1].indent && tag !== lists[lists.length - 1].tag))) lists.pop();
      let top = lists[lists.length - 1];
      if (!top || indent > top.indent) {
        const el = h(tag);
        (top ? top.li : out).append(el);
        top = { indent, tag, el, li: null };
        lists.push(top);
      }
      const li = h('li');
      if (box) {
        text = text.slice(box[0].length);
        li.className = 'task';
        const check = h('input', { type: 'checkbox', disabled: true, checked: box[1] !== ' ', 'aria-label': box[1] !== ' ' ? t('md.done') : t('md.todo') });
        li.append(check, inline(h('span'), text));
      } else {
        inline(li, text);
      }
      top.el.append(li);
      top.li = li;
    } else if (lists.length && /^\s/.test(raw)) {
      const li = lists[lists.length - 1].li;
      const target = li.classList.contains('task') ? li.lastChild : li;
      target.append(h('br'));
      inline(target, raw.trim());  // a continuation of the open list item
    } else {
      if (quote.length || lists.length) flush();
      para.push(raw.trim());
    }
  }
  if (fence) out.append(h('pre', {}, h('code', {}, fence.join('\n'))));
  flush();
  return out;
}

/** Inline markdown of ``text`` appended to ``parent`` as nodes; returns ``parent``. */
function inline(parent, text) {
  const re = new RegExp(MD_INLINE.source, 'g');
  let pos = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > pos) parent.append(text.slice(pos, m.index));
    if (m[1] != null) parent.append(h('code', {}, m[1]));
    else if (m[2] != null) {
      const url = safeUrl(m[3]);
      if (url) parent.append(inline(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }), m[2]));
      else inline(parent, m[2]);  // javascript:, data:… keep their label as plain text
    } else if (m[4] != null || m[5] != null) parent.append(inline(h('strong'), m[4] ?? m[5]));
    else if (m[6] != null) parent.append(inline(h('del'), m[6]));
    else if (m[7] != null || m[8] != null) parent.append(inline(h('em'), m[7] ?? m[8]));
    else if (m[9] != null) {
      const url = safeUrl(m[9]);
      parent.append(url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, m[9]) : m[9]);
    }
    pos = re.lastIndex;
  }
  if (pos < text.length) parent.append(text.slice(pos));
  return parent;
}

/** Markdown as plain text (an e-mail body for mailto: and the clipboard). */
function plainText(md) {
  return String(md || '')
    .replace(/\[([^\]\n]+)\]\(([^()\s]+)\)/g, '$1 ($2)')
    .replace(/\*\*(\S(?:.*?\S)?)\*\*|__(\S(?:.*?\S)?)__/g, (m, a, b) => a ?? b)
    .replace(/^#{1,6}\s+/gm, '');
}

/** The notes start with the title, date line and summary the page already shows above them. */
function notesBody(md, meeting) {
  const rows = String(md || '').replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  const skipBlank = () => { while (i < rows.length && !rows[i].trim()) i++; };
  skipBlank();
  if (i < rows.length && /^#\s/.test(rows[i])) { i++; skipBlank(); }
  if (i < rows.length && /^\*[^*].*\*(\s·.*)?$/.test(rows[i].trim())) { i++; skipBlank(); }
  if (meeting.tldr && i < rows.length && rows[i].trim() === '> ' + meeting.tldr.trim()) { i++; }
  return rows.slice(i).join('\n');
}

// ======================================================================== Meetings

function renderMeetings(prev) {
  const folder = S.route.parts[0];
  if (folder) return renderDetail(folder, prev);
  S.detail = null;
  const root = $('screen-meetings');
  const search = h('input', { type: 'search', id: 'meeting-search', placeholder: t('meetings.search_ph'), 'aria-label': t('meetings.search'), value: S.meetings.q });
  search.addEventListener('input', debounce(() => { S.meetings.q = search.value.trim(); loadMeetings(); }, 250));
  MV.list = h('div', { id: 'meeting-list', 'aria-live': 'polite' });
  MV.sub = h('p', { class: 'sub' }, t('meetings.sub'));
  fill(root,
    h('div', { class: 'screen-head' },
      h('div', {}, h('h1', {}, t('meetings.title')), MV.sub),
      btn(t('meetings.show_folder'), { onclick: () => reveal('data_dir') }, 'folder')),
    h('div', { class: 'toolbar' }, h('div', { class: 'search' }, icon('search'), search)),
    MV.list);
  if (S.meetings.loaded) renderMeetingList();
  loadMeetings();
}

const MV = {};

async function loadMeetings(more = false) {
  const q = S.meetings.q;
  const offset = more ? S.meetings.rows.length : 0;
  try {
    const data = await api('GET', `/api/meetings?limit=50&offset=${offset}&q=${enc(q)}`);
    if (q !== S.meetings.q) return;  // a newer search is under way
    S.meetings.rows = more ? S.meetings.rows.concat(data.meetings) : data.meetings;
    S.meetings.total = data.total;
    S.meetings.loaded = true;
    S.meetings.error = null;
  } catch (err) {
    S.meetings.error = err.message;
  }
  if (S.route.name === 'meetings' && !S.route.parts[0]) renderMeetingList();
}

const refreshMeetingsSoon = debounce(() => {
  if (S.route.name === 'meetings' && !S.route.parts[0]) loadMeetings();  // the list reloads whenever it is opened anyway
}, 500);

/** A meeting's state, when it needs saying: live, still recording elsewhere, interrupted, a short clip. */
function meetingChips(m) {
  const chips = [];
  if (m.live) chips.push(h('span', { class: 'pill' }, h('span', { class: 'dot-state', 'aria-hidden': 'true' }), t('meetings.live')));  // recording: its red dot
  else if (m.in_progress) chips.push(h('span', { class: 'pill warn' }, t('meetings.in_progress')));
  if (m.interrupted) chips.push(h('span', { class: 'pill warn', title: t('meetings.interrupted_note') }, t('meetings.interrupted')));
  if (m.short_clip) chips.push(h('span', { class: 'pill', title: t('meetings.short_clip_note') }, t('meetings.short_clip')));
  return chips;
}

/** Where a meeting's to-dos went, in words ("Sent to Apple Reminders"), or nothing yet. */
function meetingSent(m) {
  const pushed = Object.keys(m.pushed || {});
  return pushed.length
    ? h('span', { class: 'sent-to' }, icon('check'), t('meetings.sent_to', { tools: pushed.map(sinkLabel).join(', ') }))
    : h('span', { class: 'sent-to none' }, t('meetings.fact.not_sent'));
}

function renderMeetingList() {
  const box = MV.list;
  if (!box) return;
  const { rows, total, q, error } = S.meetings;
  if (error && !rows.length) {
    fill(box, h('div', { class: 'card empty-state' }, icon('alert', 'xl'), h('strong', {}, t('meetings.error')), humanize(error),
      btn(t('action.try_again'), { cls: 'small', id: 'meetings-retry', onclick: (e) => act(e.currentTarget, () => loadMeetings()) }, 'refresh')));
    return;
  }
  if (!rows.length) {
    fill(box, h('div', { class: 'card empty-state' }, icon(q ? 'search' : 'meetings', 'xl'),
      h('strong', {}, q ? t('meetings.no_match', { q }) : t('meetings.empty')),
      q ? '' : t('meetings.empty_text')));
    return;
  }
  // the finding: what is still waiting for you in the meetings on show (they follow the search)
  const mine = rows.reduce((n, m) => n + (m.my_actions || 0), 0);
  if (MV.sub) MV.sub.textContent = mine ? tp('meetings.finding', mine) : t('meetings.sub');
  const groups = { today: [], yesterday: [], week: [], earlier: [] };
  for (const m of rows) groups[dayBucket(m.started_at)].push(m);
  const out = [];
  for (const [key, list] of Object.entries(groups)) {
    if (!list.length) continue;
    out.push(h('section', { class: 'day-group', 'aria-labelledby': 'dg-' + key },
      h('h2', { id: 'dg-' + key }, t('meetings.group.' + key)),
      h('div', { class: 'card mlist' }, list.map((m) => meetingRow(m, key)))));
  }
  if (rows.length < total) {
    out.push(h('div', { class: 'row' }, btn(t('meetings.more', { n: fmtNum(total - rows.length) }), { onclick: (e) => act(e.currentTarget, () => loadMeetings(true)) })));
  }
  fill(box, ...out);
}

function meetingRow(m, bucket) {
  const when = bucket === 'today' || bucket === 'yesterday'
    ? h('div', { class: 'when' }, h('strong', {}, fmtClock(m.started_at)))
    : h('div', { class: 'when' }, h('strong', {}, fmtDay(m.started_at, { day: 'numeric', month: 'short' })), fmtClock(m.started_at));
  const c = m.counts || {};
  const dash = h('span', { class: 'none' }, '—');
  // one meeting per row, its figures in columns: what came out of it, where it went, how long it was
  return h('a', { class: 'mrow', href: '#/meetings/' + enc(m.folder), dataset: { folder: m.folder } },
    when,
    h('div', { class: 'what' },
      h('div', { class: 'what-top' }, h('h3', {}, m.title || t('meetings.untitled')), meetingChips(m)),
      m.tldr && h('p', { class: 'tldr' }, m.tldr)),
    h('div', { class: 'col-todos num' }, c.action ? h('span', {}, tp('count.actions', c.action)) : dash,
      m.my_actions ? h('span', { class: 'mine' }, tp('count.mine', m.my_actions)) : null),
    h('div', { class: 'col-decided num' }, c.decision ? h('span', {}, tp('count.decisions', c.decision)) : dash.cloneNode(true),
      c.question ? h('span', { class: 'muted' }, tp('count.questions', c.question)) : null),
    h('div', { class: 'col-sent' }, meetingSent(m)),
    h('div', { class: 'dur num' }, m.duration_s ? fmtDuration(m.duration_s) : '—'));
}

// ------------------------------------------------------------------ meeting detail

const TABS = ['notes', 'actions', 'transcript', 'email'];

async function renderDetail(folder, prev) {
  const same = S.detail && S.detail.folder === folder;
  if (!same) {
    S.detail = { folder, data: null, tab: 'notes', error: null, summarizing: false, showDismissed: false,
      editing: new Set(), expanded: new Set(), filter: '' };
  }
  const tab = S.route.parts[1];
  if (TABS.includes(tab)) S.detail.tab = tab;
  if (!S.detail.data) fill($('screen-meetings'), h('div', { class: 'detail-head' },
    h('a', { class: 'back', href: '#/meetings' }, icon('back'), t('meetings.all')), h('h1', {}, t('common.loading'))));
  else drawDetail();
  if (!same || !prev || prev.name !== 'meetings') await loadDetail();
}

async function loadDetail() {
  const D = S.detail;
  if (!D) return;
  try {
    D.data = await api('GET', '/api/meetings/' + enc(D.folder));
    D.error = null;
  } catch (err) {
    D.error = err;
  }
  if (S.detail === D && S.route.name === 'meetings') drawDetail();
}

const reloadDetailSoon = debounce(() => {
  const D = S.detail;
  if (!D || D.editing.size || S.route.name !== 'meetings') return;
  loadDetail();
}, 400);

function detailState(D) {
  const m = D.data.meeting;
  const inProgress = !m.ended_at;
  return { m, inProgress, readOnly: inProgress && !D.data.in_memory, liveHere: inProgress && D.data.in_memory };
}

function drawDetail() {
  const D = S.detail;
  const root = $('screen-meetings');
  const back = h('a', { class: 'back', href: '#/meetings' }, icon('back'), t('meetings.all'));
  if (D.error) {
    const missing = D.error.status === 404;
    fill(root, h('div', { class: 'detail-head' }, back),
      h('div', { class: 'card empty-state' }, icon('alert', 'xl'),
        h('h1', { class: 'sr-only' }, t('meetings.error')),
        h('strong', {}, missing ? t('meetings.gone') : t('meetings.error')), missing ? '' : humanize(D.error.message)));
    return;
  }
  const { m, readOnly, liveHere, inProgress } = detailState(D);
  const title = m.title || t('meetings.untitled');
  document.title = title + ' · Voicestream';
  let duration = m.segments && m.segments.length ? Math.max(...m.segments.map((s) => s.t_end)) : m.ended_at ? (toDate(m.ended_at) - toDate(m.started_at)) / 1000 : 0;
  if (inProgress) duration = Math.max(duration, (Date.now() - toDate(m.started_at)) / 1000);  // so far, as the sidebar counts it
  // how long, in what language, with whom: the facts beside the tabs say it; when the window is too
  // narrow for them to be beside (app.css), they go under the tabs, and the title's line says it too
  const lang = langName(m.language);
  const meta = [
    h('span', {}, fmtLongDate(m.started_at)),
    h('span', {}, fmtClock(m.started_at) + (m.ended_at ? '–' + fmtClock(m.ended_at) : '')),
    duration > 0 && fmtDuration(duration),
    lang,
    m.participants && m.participants.length && t('meetings.with', { names: m.participants.join(', ') }),
  ].filter(Boolean);
  const mac = S.app && S.app.platform === 'darwin';
  const actions = h('div', { class: 'row' },
    btn(t('meetings.push'), { id: 'push-meeting', disabled: readOnly, onclick: (e) => pushMeeting(e.currentTarget) }, 'send'),
    btn(D.summarizing ? t('meetings.rewriting') : t('meetings.rewrite'), {
      id: 'rewrite-notes', disabled: inProgress || D.summarizing, title: t('meetings.rewrite_help'),
      onclick: rewriteNotes,
    }, D.summarizing ? null : 'refresh'),
    btn(mac ? t('meetings.finder') : t('meetings.folder'), { onclick: () => reveal('meeting', D.folder) }, 'folder'));
  if (D.summarizing) actions.children[1].prepend(spinner());

  const notes = [];
  if (readOnly) notes.push(notice('warn', 'info', t('meetings.readonly')));
  if (m.interrupted) notes.push(notice('info', 'info', t('meetings.interrupted_note')));
  if (m.short_clip) notes.push(notice('info', 'info', t('meetings.short_clip_note')));
  if (liveHere) notes.push(notice('info', 'mic', t('meetings.live_here'), btn(t('meetings.go_live'), { cls: 'small', onclick: () => go('#/live') })));

  const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': t('meetings.sections') });
  const panel = h('div', { class: 'tabpanel', role: 'tabpanel', id: 'detail-panel', tabindex: '0' });
  const counts = Object.values(m.items || {}).filter((i) => !i.dismissed).length;
  for (const name of TABS) {
    const selected = D.tab === name;
    const tabEl = h('button', {
      type: 'button', class: 'tab', role: 'tab', id: 'tab-' + name, 'aria-selected': selected ? 'true' : 'false',
      'aria-controls': 'detail-panel', tabindex: selected ? '0' : '-1',
      onclick: () => selectTab(name),
    }, t('meetings.tab.' + name), name === 'actions' && counts ? h('span', { class: 'count' }, fmtNum(counts)) : null);
    tabs.append(tabEl);
  }
  tabs.addEventListener('keydown', (e) => {
    const i = TABS.indexOf(D.tab);
    const next = e.key === 'ArrowRight' ? TABS[(i + 1) % TABS.length] : e.key === 'ArrowLeft' ? TABS[(i + TABS.length - 1) % TABS.length]
      : e.key === 'Home' ? TABS[0] : e.key === 'End' ? TABS[TABS.length - 1] : null;
    if (next) { e.preventDefault(); selectTab(next, true); }
  });
  panel.setAttribute('aria-labelledby', 'tab-' + D.tab);
  panel.append(detailPanel(D));

  // the meeting as it went: who spoke when, and where each to-do and decision was said
  const voices = new VoiceStrip('detail');
  for (const seg of m.segments || []) voices.add(seg, false);
  voices.setItems(Object.values(m.items || {}));  // and its legend, once
  voices.setTotal(duration);

  fill(root,
    h('div', { class: 'detail-head' }, back, h('h1', {}, title),
      // each part after the first with its "·", so no line of it ends on one
      h('div', { class: 'meta-line' }, meta.map((el, i) => (i
        ? h('span', { class: 'with-sep' + (typeof el === 'string' ? ' facts-here' : '') }, h('span', { 'aria-hidden': 'true', class: 'sep' }, '·'), el)
        : el))),
      m.tldr && h('p', { class: 'tldr' }, m.tldr), actions),
    ...notes.map((n) => h('div', { class: 'readonly-note' }, n)),
    (m.segments || []).length ? voices.el : null,
    h('div', { class: 'detail-grid' }, h('div', { class: 'tabwrap' }, tabs, panel), detailFacts(m, duration)));
}

/** A meeting's language by name: "French", or its code when Voicestream has no name for it. */
function langName(code) {
  return code ? (has('lang.name.' + code.slice(0, 2)) ? t('lang.name.' + code.slice(0, 2)) : code) : '';
}

/** The meeting's facts beside its tabs: how long, in what language, with whom, what came out, where it went. */
function detailFacts(m, duration) {
  const items = Object.values(m.items || {}).filter((i) => !i.dismissed);
  const todos = items.filter((i) => i.kind === 'action');
  const mine = todos.filter((i) => i.status === 'open' && isMine(i.owner)).length;
  const decisions = items.filter((i) => i.kind === 'decision').length;
  const pushed = {};
  for (const i of items) for (const sink of Object.keys(i.pushed || {})) pushed[sink] = true;
  const lang = langName(m.language);
  const dash = h('span', { class: 'none' }, '—');
  const rows = [
    ['length', duration > 0 ? fmtDuration(duration) : dash],
    ['language', lang || dash.cloneNode(true)],
    ['with', m.participants && m.participants.length ? h('span', { class: 'people' }, m.participants.map((n) => person(n, isMine(n)))) : dash.cloneNode(true)],
    ['items', todos.length ? [tp('count.actions', todos.length), mine ? h('span', { class: 'mine' }, ' · ' + tp('count.mine', mine)) : null] : '0'],
    ['decisions', fmtNum(decisions)],
    ['sent', Object.keys(pushed).length ? Object.keys(pushed).map(sinkLabel).join(', ') : h('span', { class: 'none' }, t('meetings.fact.not_sent'))],
  ];
  return h('aside', { class: 'facts', 'aria-labelledby': 'facts-h' }, h('h2', { id: 'facts-h' }, t('meetings.facts')),
    h('dl', { class: 'kv' }, rows.flatMap(([key, value]) => [h('dt', {}, t('meetings.fact.' + key)), h('dd', {}, value)])));
}

function selectTab(name, focus) {
  const D = S.detail;
  D.tab = name;
  history.replaceState(null, '', '#/meetings/' + enc(D.folder) + '/' + name);
  S.route = parseRoute();
  drawDetail();
  if (focus) $('tab-' + name).focus();
}

function detailPanel(D) {
  const { m } = detailState(D);
  if (D.tab === 'notes') {
    const md = D.data.notes_markdown;
    if (!md) {
      return h('div', { class: 'empty-state' }, icon('sparkle', 'xl'), h('strong', {}, t('meetings.no_notes')),
        m.ended_at ? t('meetings.no_notes_text') : t('meetings.no_notes_live'));
    }
    return h('div', { class: 'prose', id: 'notes' }, renderMarkdown(notesBody(md, m)));
  }
  if (D.tab === 'actions') return actionsPanel(D);
  if (D.tab === 'transcript') return transcriptPanel(D);
  return emailPanel(D);
}

function detailCtx(D) {
  const items = new Map(Object.entries(D.data.meeting.items || {}));
  return {
    id: 'detail', live: false, items, editing: D.editing, expanded: D.expanded, showDismissed: D.showDismissed,
    sinks: () => null,
    readOnly: () => detailState(D).readOnly,
    path: (item, action) => `/api/meetings/${enc(D.folder)}/items/${enc(item.id)}` + (action ? '/' + action : ''),
  };
}

function actionsPanel(D) {
  const ctx = detailCtx(D);
  const board = D.board = new ItemBoard(ctx);
  const origSet = board.set.bind(board);
  board.set = (item) => {  // keep the meeting's own copy current (the Notes tab, counts)
    origSet(item);
    if (item && item.id) D.data.meeting.items[item.id] = board.items.get(item.id);
  };
  const dismissed = h('input', { type: 'checkbox', id: 'detail-show-dismissed', checked: D.showDismissed });
  dismissed.addEventListener('change', () => { D.showDismissed = dismissed.checked; board.showDismissed(dismissed.checked); });
  const readOnly = ctx.readOnly();
  return h('div', {},
    h('div', { class: 'row' },
      h('p', { class: 'muted grow' }, readOnly ? t('meetings.items_readonly') : t('meetings.items_help')),
      h('label', { for: 'detail-show-dismissed', class: 'row small muted' }, dismissed, t('items.show_dismissed'))),
    !readOnly && addItemForm(board, (body) => api('POST', `/api/meetings/${enc(D.folder)}/items`, body)),
    board.el);
}

function detailUpsert(item) {
  const D = S.detail;
  if (!D || !D.data) return;
  D.data.meeting.items[item.id] = item;
  if (D.board && D.tab === 'actions') D.board.set(item);
}

function transcriptPanel(D) {
  const segs = D.data.meeting.segments || [];
  if (!segs.length) return h('div', { class: 'empty-state' }, icon('mic', 'xl'), h('strong', {}, t('meetings.no_transcript')));
  const filter = h('input', { type: 'search', placeholder: t('meetings.find_ph'), 'aria-label': t('meetings.find'), value: D.filter });
  const list = h('div', { class: 'tlines', id: 'transcript-lines' });
  const draw = () => {
    const needle = D.filter.toLowerCase();
    const rows = segs.filter((s) => !needle || s.text.toLowerCase().includes(needle) || (s.speaker || '').toLowerCase().includes(needle));
    fill(list, ...rows.map((s) => h('div', { class: 'tline ' + (s.source === 'mic' ? 'me' : 'others'), dataset: { seg: s.id } },
      h('span', { class: 'ts', title: fmtClock(s.wall_time) }, fmtTs(s.t_start)),
      h('div', { class: 'body' }, h('span', { class: 'spk' }, speakerOf(s)), h('span', {}, s.text)))));
    for (const item of Object.values(D.data.meeting.items || {})) markSaid(list, item);
    if (!rows.length) list.append(h('p', { class: 'muted' }, t('meetings.find_none')));
  };
  filter.addEventListener('input', debounce(() => { D.filter = filter.value.trim(); draw(); }, 150));
  draw();
  return h('div', {}, h('div', { class: 'toolbar' }, h('div', { class: 'search' }, icon('search'), filter),
    h('span', { class: 'muted small' }, tp('meetings.lines', segs.length))), list);
}

function emailPanel(D) {
  const e = D.data.meeting.followup_email;
  if (!e) {
    return h('div', { class: 'empty-state' }, icon('mail', 'xl'), h('strong', {}, t('email.none')),
      D.data.meeting.ended_at ? t('email.none_text') : t('meetings.no_notes_live'));
  }
  const body = plainText(e.body_markdown);
  const to = (e.to || []).join(', ');
  const href = 'mailto:' + (e.to || []).map((a) => enc(a).replace(/%40/g, '@')).join(',')
    + '?subject=' + enc(e.subject || '') + '&body=' + enc(body);
  return h('div', {},
    h('div', { class: 'email-grid' },
      h('span', { class: 'k' }, t('email.to')), h('div', {}, h('p', { class: 'email-to' }, to || t('email.to_none'))),
      h('span', { class: 'k' }, t('email.subject')), h('p', { class: 'email-subject' }, h('strong', {}, e.subject || '')),
      h('span', { class: 'k top' }, t('email.body')), h('div', { class: 'email-body prose', id: 'email-body' }, renderMarkdown(e.body_markdown))),
    h('div', { class: 'row' },
      btn(t('email.copy'), { id: 'copy-email', onclick: (ev) => act(ev.currentTarget, async () => {
        await copyText((e.subject ? e.subject + '\n\n' : '') + body);
        toast(t('email.copied'), 'ok');
      }) }, 'copy'),
      h('a', { class: 'btn', id: 'mail-link', href, target: '_blank', rel: 'noopener noreferrer' }, icon('mail'), t('email.open_mail')),
      h('span', { class: 'muted small' }, t('email.never_sent'))));
}

async function pushMeeting(button) {
  const D = S.detail;
  S.pushingFolder = D.folder;
  const res = await act(button, () => api('POST', `/api/meetings/${enc(D.folder)}/push`, {}));
  S.pushingFolder = null;
  if (!res) return;
  const sent = new Set(), failed = new Set();
  for (const per of Object.values(res.items || {})) {
    for (const [sink, ref] of Object.entries(per)) (ref === 'error' ? failed : sent).add(sinkLabel(sink));
  }
  for (const sink of Object.keys(res.notes || {})) sent.add(sinkLabel(sink));
  if (res.errors && res.errors.length) {
    const noTools = res.errors.some((e) => /no tools are enabled/.test(e));
    toast(noTools ? humanize(res.errors[0]) : res.errors.slice(0, 3).map(humanize).join('\n'), 'error', t('toast.push_problem'),
      noTools ? { label: t('fix.tools_settings'), run: () => go('#/settings/tools') } : null);
  } else if (sent.size) {
    toast(t('toast.pushed_meeting', { tools: [...sent].join(', ') }), 'ok', t('toast.pushed'));
  } else {
    toast(t('toast.up_to_date'), 'ok');
  }
  reloadDetailSoon();
}

async function rewriteNotes() {
  const D = S.detail;
  // waiting before the request is sent: offline, the notes can be rewritten before it even answers
  D.summarizing = true;
  drawDetail();
  const ok = await act(null, () => api('POST', `/api/meetings/${enc(D.folder)}/summarize`));
  if (ok === undefined && S.detail === D) {
    D.summarizing = false;
    drawDetail();
  }
}

function onNotesUpdated(d) {
  const D = S.detail;
  if (!D || d.folder !== D.folder || !D.summarizing) return;
  D.summarizing = false;
  if (d.error) toast(humanize(d.error), 'error', t('toast.rewrite_failed'));
  else toast(t('toast.rewritten'), 'ok');
  loadDetail();
}

/** The meeting whose page is open has ended (``meeting.ended``), or its late agents finished
 *  (``meeting.notes_updated`` without a folder): reload it, under the folder's new name — it is
 *  renamed after the meeting's title then, and the old name no longer exists. */
function onDetailMeetingDone(d) {
  const D = S.detail;
  if (!D || !D.data || !d.meeting_id || d.meeting_id !== D.data.meeting.id || d.folder !== undefined) return;
  const folder = basename(d.dir) || (d.notes ? basename(String(d.notes).replace(/[\\/][^\\/]*$/, '')) : '') || D.folder;
  if (folder !== D.folder) {
    D.folder = folder;
    if (S.route.name === 'meetings') {
      const tab = S.route.parts[1];
      history.replaceState(null, '', '#/meetings/' + enc(folder) + (tab ? '/' + enc(tab) : ''));
      S.route = parseRoute();
    }
  }
  if (S.route.name === 'meetings' && !D.editing.size) loadDetail();  // never under an edit you are typing
}

function onMeetingSaved(d) {
  if (S.detail && d.folder === S.detail.folder && !S.detail.summarizing) reloadDetailSoon();
  if (S.lastMeeting && S.lastMeeting.folder === d.folder) { S.lastMeetingLoaded = false; refreshLast(); }
  refreshMeetingsSoon();
}

// =========================================================================== Costs

const SERIES = [['stt_usd', 'cost.stt'], ['llm_usd', 'cost.claude'], ['agents_usd', 'cost.agents']];

async function renderCosts() {
  const root = $('screen-costs');
  if (!CV.data) fill(root, h('div', { class: 'screen-head' }, h('div', {}, h('h1', {}, t('costs.title')), h('p', { class: 'sub' }, t('common.loading')))));
  try {
    CV.data = await api('GET', '/api/cost?hours=' + S.costHours);
  } catch (err) {
    fill(root, h('div', { class: 'screen-head' }, h('h1', {}, t('costs.title'))),
      h('div', { class: 'card empty-state' }, icon('alert', 'xl'), h('strong', {}, t('costs.error')), humanize(err.message),
        btn(t('action.try_again'), { cls: 'small', id: 'costs-retry', onclick: () => renderCosts() }, 'refresh')));
    return;
  }
  if (S.route.name === 'costs') drawCosts();
}

const CV = {};

function drawCosts() {
  const root = $('screen-costs');
  const d = CV.data;
  const today = d.today || {};
  const total = today.total_usd ?? ((today.stt_usd || 0) + (today.llm_usd || 0) + (today.agents_usd || 0));
  const capped = (today.stt_usd || 0) + (today.llm_usd || 0);
  const budget = d.budget || {};
  const frac = budget.daily_usd ? capped / budget.daily_usd : 0;
  const track = h('div', { class: 'track' }, h('span'));
  track.firstChild.style.width = Math.min(100, frac * 100).toFixed(1) + '%';

  // the 30 days lead (the screen's answer), by service; today, against the daily budget, under them
  const history = d.history || [];
  const month = history.reduce((a, r) => a + dayTotal(r), 0);
  const parts = SERIES.map(([key, label]) => ({ key, label: t(label), usd: history.reduce((a, r) => a + (r[key] || 0), 0) }))
    .sort((a, b) => b.usd - a.usd);
  const most = parts[0].usd || 1;
  const monthCard = h('section', { class: 'card stats', 'aria-labelledby': 'month-h' },
    h('div', { class: 'stat hero-stat' },
      h('h2', { class: 'label', id: 'month-h' }, t('costs.month')),
      h('div', { class: 'value num', id: 'cost-month' }, money(month)),
      h('div', { class: 'small muted' }, t('costs.per_day_avg', { amount: money(month / Math.max(1, history.length)) }))),
    // most costly first, a thin bar under each: the top one in ink, the others grey
    h('ul', { class: 'bar-list', 'aria-label': t('costs.by_service') }, parts.map((part, i) => {
      const bar = h('span', { class: 'bar' + (i === 0 && part.usd ? ' top' : '') });
      bar.style.width = ((part.usd / most) * 100).toFixed(1) + '%';
      return h('li', {}, h('span', { class: 'name' }, part.label), h('span', { class: 'value num' + (part.usd ? '' : ' zero') }, money(part.usd)),
        h('span', { class: 'bar-track', 'aria-hidden': 'true' }, bar));
    })));
  const todayCard = h('section', { class: 'card stats', 'aria-labelledby': 'today-h' },
    h('div', { class: 'stat' },
      h('h2', { class: 'label', id: 'today-h' }, t('costs.today')),
      h('div', { class: 'value num', id: 'cost-today' }, money(total)),
      budget.daily_usd ? h('div', { class: 'budget' + (frac >= 1 ? ' over' : frac >= 0.8 ? ' warn' : '') },
        track, h('div', { class: 'small muted' }, t('costs.of_budget', { spent: money(capped), budget: money(budget.daily_usd) })),
        today.agents_usd > 0 ? h('div', { class: 'small muted' }, t('costs.budget_excludes')) : null) : null),
    h('div', { class: 'stat-split' }, SERIES.map(([key, label]) => h('div', { class: 'stat' },
      h('div', { class: 'label' }, t(label)),
      h('div', { class: 'value num' + (today[key] ? '' : ' zero') }, money(today[key])),
      key === 'stt_usd' && today.stt_minutes ? h('div', { class: 'small muted' }, t('costs.minutes', { n: fmtNum(Math.round(today.stt_minutes)) })) : null,
      key === 'agents_usd' && budget.agents_daily_usd ? h('div', { class: 'small muted' }, t('costs.agents_cap', { amount: money(budget.agents_daily_usd) })) : null))));

  // the finding: where most of the 30 days went
  fill(root,
    h('div', { class: 'screen-head' },
      h('div', {}, h('h1', {}, t('costs.title')),
        h('p', { class: 'sub' }, month > 0 ? t('costs.finding', { service: parts[0].label, amount: money(parts[0].usd), total: money(month) }) : t('costs.sub'))),
      h('a', { class: 'btn', href: '#/settings/claude' }, t('costs.budget_link'))),
    h('div', { class: 'costs-grid' }, historyCard(history), h('div', { class: 'costs-side' }, monthCard, todayCard)),
    estimateCard(d.estimate));
}

/** A rounded top for the money axis, with four equal steps that read as round amounts. */
function niceCeil(x) {
  if (x <= 0) return 0.1;
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  for (const m of [1, 1.2, 1.6, 2, 3, 4, 6, 8, 10]) if (m * p >= x) return m * p;
  return 10 * p;
}

const dayTotal = (r) => (r.stt_usd || 0) + (r.llm_usd || 0) + (r.agents_usd || 0);

function historyCard(rows) {
  const totals = rows.map(dayTotal);
  const top = niceCeil(Math.max(0, ...totals));
  const plot = h('div', { class: 'plot' });
  const digits = top / 4 < 1 ? 2 : 0;
  for (const frac of [0, 0.25, 0.5, 0.75, 1]) {
    const y = (1 - frac) * 100;
    const g = h('div', { class: 'gridline' + (frac === 0 ? ' base' : '') });
    g.style.top = y + '%';
    // figures only: "US$" on each one ran into the plot in French, and the subtitle says "in US dollars"
    const lab = h('span', { class: 'ytick', 'aria-hidden': 'true' }, fmtFixed(top * frac, frac ? digits : 0));
    lab.style.top = y + '%';
    plot.append(g, lab);
  }
  const cols = h('div', { class: 'cols' });
  const peak = totals.indexOf(Math.max(...totals));
  rows.forEach((r, i) => {
    const day = fmtDay(r.day + 'T12:00:00', { weekday: 'short', day: 'numeric', month: 'short' });
    // one column per day: its total; today's, the day still running, in a paler tone
    const stack = h('div', { class: 'stack' });
    const pct = ((totals[i] / top) * 100).toFixed(2) + '%';
    if (totals[i] > 0) {
      const bar = h('div', { class: 'seg-bar' });
      bar.style.height = 'max(2px, ' + pct + ')';
      stack.append(bar);
    }
    const label = t('costs.bar_aria', { day, total: money(totals[i]), stt: money(r.stt_usd), claude: money(r.llm_usd), agents: money(r.agents_usd) });
    const col = h('button', { type: 'button', class: 'col' + (i === rows.length - 1 ? ' today' : ''), 'aria-label': label },
      i === peak && totals[i] > 0 ? h('span', { class: 'peak num', 'aria-hidden': 'true' }, money(totals[i])) : null, stack);
    if (i === peak) col.style.setProperty('--hpct', pct);
    const show = () => showTip(col, day, r, totals[i]);
    col.addEventListener('pointerenter', show);
    col.addEventListener('focus', show);
    col.addEventListener('pointerleave', hideTip);
    col.addEventListener('blur', hideTip);
    cols.append(col);
  });
  plot.append(cols);
  // a label every week, counted back from today so the last one is today's
  const xl = h('div', { class: 'xlabels', 'aria-hidden': 'true' }, rows.map((r, i) =>
    h('span', {}, (rows.length - 1 - i) % 7 === 0 ? fmtDay(r.day + 'T12:00:00', { day: 'numeric', month: 'short' }) : '')));
  const empty = totals.every((v) => v === 0);
  const table = h('details', { class: 'small' }, h('summary', { class: 'muted' }, t('costs.as_table')),
    h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, h('th', {}, t('costs.day')), SERIES.map(([, label]) => h('th', { class: 'num' }, t(label))), h('th', { class: 'num' }, t('costs.total')))),
      h('tbody', {}, rows.filter((r, i) => totals[i] > 0).reverse().map((r) => h('tr', {},
        h('td', {}, fmtDay(r.day + 'T12:00:00')), SERIES.map(([key]) => h('td', { class: 'num' }, money(r[key]))),
        h('td', { class: 'num' }, money(dayTotal(r))))))));
  return h('section', { class: 'card card-pad history', 'aria-labelledby': 'hist-h' },
    h('div', { class: 'panel-head' }, h('div', { class: 'grow' }, h('h2', { id: 'hist-h' }, t('costs.history')),
      h('p', { class: 'muted small' }, t('costs.per_day_usd'))),
      h('div', { class: 'legend' }, h('span', {}, h('i', { class: 'key day' }), t('costs.key_day')),
        h('span', {}, h('i', { class: 'key today' }), t('costs.key_today')))),
    empty && h('p', { class: 'muted small' }, t('costs.history_empty')),
    h('div', { class: 'chart', role: 'group', 'aria-label': t('costs.history') }, plot, xl),
    table);
}

function showTip(anchor, day, r, total) {
  const tip = $('tooltip');
  fill(tip, h('div', { class: 'tt-head' }, day),
    h('div', { class: 'tt-row' }, h('strong', {}, money(total)), t('costs.total')),
    ...SERIES.map(([key, label]) => h('div', { class: 'tt-row' }, h('strong', {}, money(r[key])), t(label))));
  tip.hidden = false;
  const box = anchor.getBoundingClientRect(), tb = tip.getBoundingClientRect();
  const left = Math.min(window.innerWidth - tb.width - 8, Math.max(8, box.left + box.width / 2 - tb.width / 2));
  const top = Math.max(8, box.top - tb.height - 8);
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function hideTip() { $('tooltip').hidden = true; }

function sttName(e) {
  const key = 'costs.backend.' + e.backend;
  return has(key) ? t(key) : e.backend;
}

function estimateCard(e) {
  const out = h('output', { for: 'hours', id: 'hours-out' }, t('costs.hours', { n: fmtNum(S.costHours) }));
  const slider = h('input', { type: 'range', id: 'hours', min: 1, max: 8, step: 0.5, value: S.costHours, 'aria-label': t('costs.hours_label') });
  const linesBox = h('div', { class: 'lines', id: 'estimate-lines' });
  const showEstimate = (est) => {
    const rows = [
      [t('costs.est_stt', { name: sttName(est) }), est.stt_usd],
      [t('costs.est_extraction'), est.extraction_usd],
      [t('costs.est_summaries'), est.summaries_usd],
    ];
    fill(linesBox, 
      ...rows.map(([label, v]) => h('div', {}, h('span', {}, label), h('span', { class: 'num' + (v ? '' : ' zero') }, v ? money(v, 2) : t('costs.free')))),
      h('div', { class: 'total' }, h('span', {}, t('costs.per_day')), h('span', { id: 'estimate-day', class: 'num' }, money(est.total_usd, 2))),
      h('div', {}, h('span', { class: 'muted' }, t('costs.per_month')), h('span', { id: 'estimate-month', class: 'num' }, money(est.per_month_usd, est.per_month_usd >= 10 ? 0 : 2))));
  };
  showEstimate(e);
  const refetch = debounce(async () => {
    try {
      const d = await api('GET', '/api/cost?hours=' + S.costHours);
      CV.data = d;
      showEstimate(d.estimate);
    } catch (_) { /* keep the previous numbers */ }
  }, 200);
  slider.addEventListener('input', () => {
    S.costHours = Number(slider.value);
    out.textContent = t('costs.hours', { n: fmtNum(S.costHours) });
    refetch();
  });
  const free = !e.stt_usd;
  const offline = !e.extraction_usd && !e.summaries_usd;
  return h('section', { class: 'card estimate', 'aria-labelledby': 'est-h' },
    h('h2', { id: 'est-h' }, t('costs.estimate')),
    h('div', { class: 'estimate-grid' },
      h('div', {},
        h('label', { for: 'hours', class: 'muted' }, t('costs.hours_label')),
        h('div', { class: 'slider' }, slider, out),
        linesBox),
      h('div', { class: 'explain small' },
        h('p', {}, free ? t('costs.explain_free') : t('costs.explain_cloud', { name: sttName(e) })),
        h('p', {}, offline ? t('costs.explain_offline') : t('costs.explain_claude')),
        h('p', {}, t('costs.explain_agents')),
        h('p', {}, h('a', { href: '#/settings/agents' }, t('costs.agents_link'))))));
}

// ======================================================================== Settings

const SECTIONS = ['general', 'stt', 'claude', 'audio', 'tools', 'agents', 'advanced'];
// which section shows a setting (the longest prefix wins), so a refused save opens the right one
const SECTION_PREFIXES = {
  general: ['user_name', 'transcription.language', 'transcription.whisper_cpp.allowed_languages', 'brain.language', 'always_on', 'timezone', 'pref:'],
  stt: ['transcription', 'secret:OPENAI_API_KEY', 'secret:DEEPGRAM_API_KEY'],
  claude: ['brain', 'budget.daily_usd', 'secret:ANTHROPIC_API_KEY'],
  audio: ['capture'],
  tools: ['sinks', 'secret:'],
  agents: ['agents', 'budget.agents_daily_usd'],
  advanced: ['data_dir', 'dashboard', 'pricing', 'vad', 'echo', 'screen', 'dispatch', 'budget'],
};
const MAIN_TOOLS = ['reminders', 'markdown', 'google', 'hubspot', 'shortcuts', 'command'];

function sectionFor(path) {
  let best = 'advanced', len = -1;
  for (const [sec, prefixes] of Object.entries(SECTION_PREFIXES)) {
    for (const p of prefixes) {
      const hit = path === p || path.startsWith(p.endsWith(':') ? p : p + '.');
      if (hit && p.length > len) { best = sec; len = p.length; }
    }
  }
  return best;
}

async function loadSettings() {
  S.settings = await api('GET', '/api/settings');
  if (S.settings.values.user_name) setUserName(S.settings.values.user_name);
}

/** Your name, which says which to-dos and people are you: a meeting's page drawn before it was known says it again. */
function setUserName(name) {
  if (name === S.userName) return;
  S.userName = name;
  const D = S.detail;
  if (S.route.name === 'meetings' && D && D.data && !D.editing.size) drawDetail();
  if (S.route.name === 'live' && LV.root && !inMeetingView()) renderLastMeeting();
}

async function loadModels() {
  S.models = await api('GET', '/api/models');
  for (const m of S.models.models) {
    if (m.downloading) S.downloads[m.name] = S.downloads[m.name] || m.downloading;
    else if (S.downloads[m.name] && m.installed) delete S.downloads[m.name];
  }
}

// ---------------------------------------------------------------- values & drafts

function dig(obj, keys) {
  let cur = obj;
  for (const k of keys) { if (cur == null || typeof cur !== 'object') return undefined; cur = cur[k]; }
  return cur;
}

function savedVal(path) {
  const v = S.settings && S.settings.values;
  const keys = path.split('.');
  if (keys[0] === 'sinks') {
    const sk = S.settings && S.settings.sinks[keys[1]];
    if (!sk) return keys[2] === 'enabled' ? false : undefined;
    if (keys.length === 3 && keys[2] === 'enabled') return sk.enabled;
    return dig(sk.values, keys.slice(2));
  }
  return dig(v, keys);
}

function val(path) {
  if (path in S.draft) return S.draft[path];
  if (S.heldStt && path in S.heldStt.changes) return S.heldStt.changes[path];  // saved, applied once downloaded
  return savedVal(path);
}

function same(a, b) { return JSON.stringify(a ?? null) === JSON.stringify(b ?? null); }

function setDraft(path, value) {
  if (S.heldStt && path in S.heldStt.changes) S.heldStt = null;  // a new choice replaces the one waiting
  if (same(value, savedVal(path))) delete S.draft[path];
  else S.draft[path] = value;
  if (S.fieldErrors[path]) { delete S.fieldErrors[path]; markField(path, null); }
  S.saveError = null;
  renderSaveBar();
}

function pref(key) { return key in S.prefsDraft ? S.prefsDraft[key] : S.app.prefs[key]; }

function setPref(key, value) {
  if (value === S.app.prefs[key]) delete S.prefsDraft[key];
  else S.prefsDraft[key] = value;
  renderSaveBar();
}

function dirty() { return Object.keys(S.draft).length + Object.keys(S.prefsDraft).length > 0; }

const fid = (path) => 'f-' + path.replace(/[^\w-]/g, '_');

// ------------------------------------------------------------------- form parts

function friendlyError(path, msg) {
  const text = String(msg || '').replace(/^\[sinks\.[\w-]+\]\s*/, '').replace(/^[\w.-]+:\s+/, '');
  if (/at least 1 character/.test(text)) return t('err.required');
  if (/valid (number|integer)|unable to parse/i.test(text)) return t('err.number');
  if (/should match pattern/.test(text)) return t('err.format');
  if (/item_shortcut, update_shortcut/.test(text)) return t('err.shortcuts');
  if (/item_command, update_command/.test(text)) return t('err.command');
  if (/greater than or equal|less than or equal|greater than|less than/.test(text)) return t('err.range', { detail: text });
  return text;
}

function errorEl(path) {
  const err = S.fieldErrors[path];
  return h('p', { class: 'field-error', id: fid(path) + '-err', role: err ? 'alert' : null }, err ? friendlyError(path, err) : '');
}

/** Show (or with ``null`` clear) a field's error without redrawing the section. */
function markField(path, msg) {
  const wrap = document.querySelector(`[data-path="${CSS.escape(path)}"]`);
  if (!wrap) return;
  wrap.classList.toggle('invalid', Boolean(msg));
  const err = wrap.querySelector(':scope > .field-error, :scope .tool-error');
  if (err) err.textContent = msg ? friendlyError(path, msg) : '';
  for (const input of wrap.querySelectorAll('input, select, textarea')) {
    if (msg) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
  }
}

function field(path, label, control, { help, extra, cls = '' } = {}) {
  const err = S.fieldErrors[path];
  const describedBy = [help && fid(path) + '-help', fid(path) + '-err'].filter(Boolean).join(' ');
  for (const el of control.matches && control.matches('input, select, textarea') ? [control] : []) {
    el.setAttribute('aria-describedby', describedBy);
    if (err) el.setAttribute('aria-invalid', 'true');
  }
  return h('div', { class: 'field ' + cls + (err ? ' invalid' : ''), dataset: { path } },
    h('label', { for: control.id || null }, label),
    h('div', { class: 'control' }, control, extra),
    help && h('p', { class: 'help', id: fid(path) + '-help' }, help),
    errorEl(path));
}

function textInput(path, { type = 'text', placeholder = '', nullIfEmpty = true, cls = '' } = {}) {
  const cur = val(path);
  const el = h('input', { type, id: fid(path), class: cls, value: cur == null ? '' : Array.isArray(cur) ? cur.join(' ') : String(cur), placeholder, autocomplete: 'off', spellcheck: 'false' });
  el.addEventListener('input', () => setDraft(path, el.value.trim() === '' && nullIfEmpty ? null : el.value.trim()));
  return el;
}

function numberInput(path, { min, max, step = 'any' } = {}) {
  const cur = val(path);
  const el = h('input', { type: 'number', id: fid(path), class: 'narrow', value: cur == null ? '' : String(cur), min, max, step, inputmode: 'decimal' });
  el.addEventListener('input', () => {
    const raw = el.value.trim();
    const n = Number(raw);
    setDraft(path, raw === '' ? null : Number.isFinite(n) ? n : raw);
  });
  return el;
}

function selectInput(path, options, { onChange } = {}) {
  const cur = val(path);
  const el = h('select', { id: fid(path) }, options.map(([value, label], i) => h('option', { value: String(i), selected: same(value, cur) || null }, label)));
  if (!options.some(([value]) => same(value, cur))) {
    el.append(h('option', { value: '-1', selected: true }, String(cur)));
  }
  el.addEventListener('change', () => {
    const i = Number(el.value);
    if (i >= 0) setDraft(path, options[i][0]);
    if (onChange) onChange();
  });
  return el;
}

function switchRow(id, label, help, checked, onChange, { disabled = false, path } = {}) {
  const input = h('input', { type: 'checkbox', role: 'switch', class: 'switch', id, checked, disabled });
  input.addEventListener('change', () => onChange(input.checked));
  return h('div', { class: 'switch-row', dataset: path ? { path } : null },
    h('div', { class: 'text' }, h('label', { for: id }, h('strong', {}, label)), help && h('span', { id: id + '-help' }, help)),
    input);
}

function settingSwitch(path, label, help, { on = true, off = false, redraw = false, disabled = false } = {}) {
  return switchRow(fid(path), label, help, same(val(path), on), (checked) => {
    setDraft(path, checked ? on : off);
    if (redraw) redrawSection();
  }, { disabled, path });
}

function prefSwitch(key, label, help, { disabled = false } = {}) {
  return switchRow('p-' + key, label, help, Boolean(pref(key)), (checked) => setPref(key, checked), { disabled, path: 'pref:' + key });
}

function choiceCards(name, options, current, onPick, { list = false, cols = 0 } = {}) {
  return h('div', { class: 'choices' + (list ? ' list' : '') + (cols === 2 ? ' two' : ''), role: 'radiogroup', 'aria-label': name },
    options.map(({ value, title, desc, disabled }) => {
      const id = 'c-' + name.replace(/\W/g, '_') + '-' + String(value).replace(/\W/g, '_');
      // named by the title alone, with the description read after it
      const input = h('input', { type: 'radio', name: 'c-' + name, id, value: String(value), checked: value === current, disabled,
        'aria-labelledby': id + '-t', 'aria-describedby': desc ? id + '-d' : null });
      input.addEventListener('change', () => { if (input.checked) onPick(value); });
      return h('label', { class: 'choice' + (value === current ? ' selected' : ''), for: id },
        input, h('span', { class: 'choice-body' }, h('strong', { id: id + '-t' }, title), desc && h('span', { class: 'desc', id: id + '-d' }, desc)));
    }));
}

// ------------------------------------------------------------- languages, models

const LANG_CHOICES = {
  'fr+en': { 'transcription.language': null, 'transcription.whisper_cpp.allowed_languages': ['fr', 'en'], 'brain.language': 'auto' },
  fr: { 'transcription.language': 'fr', 'transcription.whisper_cpp.allowed_languages': ['fr'], 'brain.language': 'fr' },
  en: { 'transcription.language': 'en', 'transcription.whisper_cpp.allowed_languages': ['en'], 'brain.language': 'en' },
  any: { 'transcription.language': null, 'transcription.whisper_cpp.allowed_languages': [], 'brain.language': 'auto' },
};

function langChoice(get = val) {
  const lang = get('transcription.language');
  const allowed = get('transcription.whisper_cpp.allowed_languages') || [];
  if (lang === 'fr') return 'fr';
  if (lang === 'en') return 'en';
  if (!lang && allowed.length === 2 && allowed.includes('fr') && allowed.includes('en')) return 'fr+en';
  if (!lang && !allowed.length) return 'any';
  return 'other';
}

function languageCards(name, current, onPick) {
  const options = ['fr+en', 'fr', 'en', 'any'].map((value) => ({ value, title: t('lang.' + value), desc: t('lang.' + value + '.desc') }));
  if (current === 'other') {
    options.push({ value: 'other', title: t('lang.other', { code: val('transcription.language') || '' }), desc: t('lang.other.desc') });
  }
  return choiceCards(name, options, current, onPick, { cols: 2 });
}

const MODEL_LISTS = new Set();

function modelLabel(m) { return has('model.' + m.name + '.label') ? t('model.' + m.name + '.label') : m.label; }
function modelNote(m) { return has('model.' + m.name + '.note') ? t('model.' + m.name + '.note') : m.note; }

/** The on-device models with Download / progress / Remove; ``pick`` adds the radio buttons. */
function modelList(opts = {}) {
  const box = h('div', { class: 'models', role: opts.pick ? 'radiogroup' : null, 'aria-label': t('stt.models') });
  const entry = { el: box, opts };
  MODEL_LISTS.add(entry);
  fillModelList(entry);
  return box;
}

function fillModelList({ el, opts }) {
  if (!S.models) { fill(el, h('p', { class: 'muted' }, t('common.loading'))); return; }
  const selected = opts.selected ? opts.selected() : val('transcription.whisper_cpp.model');
  const rows = S.models.models.filter((m) => m.kind === 'whisper' && (!opts.only || opts.only.includes(m.name)));
  fill(el, ...rows.map((m) => modelRow(m, selected, opts)));
  const vad = S.models.models.find((m) => m.kind === 'vad');
  if (vad && !opts.only) {
    el.append(h('p', { class: 'help small muted' }, icon('info'), ' ',
      vad.installed ? t('stt.vad_ok') : S.downloads[vad.name] ? t('stt.vad_downloading') : t('stt.vad_missing')));
  }
}

function refreshModelLists() {
  for (const entry of [...MODEL_LISTS]) {
    if (!entry.el.isConnected) { MODEL_LISTS.delete(entry); continue; }
    fillModelList(entry);
  }
}

function modelRow(m, selected, opts) {
  const dl = S.downloads[m.name];
  const status = h('div', { class: 'status' });
  if (dl) {
    status.append(progressBar(m.name, dl), btn(t('stt.cancel'), { cls: 'small', onclick: (e) => cancelDownload(m.name, e.currentTarget) }));
  } else if (m.installed) {
    status.append(h('span', { class: 'pill ok' }, icon('check'), t('stt.downloaded')));
    if (!opts.noRemove) status.append(btn(t('stt.remove'), { cls: 'small ghost', 'aria-label': t('stt.remove_named', { name: modelLabel(m) }), onclick: (e) => removeModel(m, e.currentTarget) }, 'trash'));
  } else {
    status.append(btn(t('stt.download_size', { size: fmtBytes(m.size) }), {
      cls: 'small', 'aria-label': t('stt.download_named', { name: modelLabel(m) }), onclick: (e) => downloadModel(m.name, e.currentTarget),
    }, 'download'));
  }
  const text = h('span', {},
    h('span', { class: 'name' }, modelLabel(m), m.recommended && modelLabel(m) !== t('stt.recommended') && h('span', { class: 'pill info' }, t('stt.recommended'))),
    h('span', { class: 'note' }, modelNote(m)),
    h('span', { class: 'note' }, ' ' + t('stt.size_ram', { size: fmtBytes(m.size), ram: fmtNum(m.ram_gb) })));
  let pick;
  if (opts.pick) {
    const id = 'm-' + m.name.replace(/\W/g, '_') + (opts.idSuffix || '');
    const input = h('input', { type: 'radio', name: 'model' + (opts.idSuffix || ''), id, checked: selected === m.name });
    input.addEventListener('change', () => { if (input.checked) opts.pick(m.name); });
    pick = h('label', { class: 'pick', for: id }, input, text);
  } else {
    pick = h('div', { class: 'pick' }, text);
  }
  return h('div', { class: 'model' + (opts.pick && selected === m.name ? ' selected' : ''), dataset: { model: m.name } }, pick, status);
}

function progressBar(name, dl) {
  const el = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', dataset: { dl: name } },
    h('div', { class: 'track' }, h('span')), h('span', { class: 'pct' }));
  updateProgress(el, dl);
  return el;
}

function updateProgress(el, dl) {
  const pct = dl.total ? Math.min(100, (dl.done / dl.total) * 100) : 0;
  const shown = Math.min(99, Math.round(pct));  // 100% is "Downloaded", once the file is checked and in place
  el.querySelector('.track span').style.transform = 'scaleX(' + (pct / 100).toFixed(3) + ')';
  el.setAttribute('aria-valuenow', String(shown));
  el.setAttribute('aria-label', t('stt.downloading'));
  el.querySelector('.pct').textContent = t('stt.progress', { pct: fmtNum(shown), done: fmtBytes(dl.done), total: fmtBytes(dl.total) });
}

async function downloadModel(name, button) {
  const res = await act(button, () => api('POST', '/api/models/' + enc(name) + '/download'));
  if (res && res.started && !S.downloads[name]) S.downloads[name] = { done: 0, total: (S.models.models.find((m) => m.name === name) || {}).size || 0 };
  refreshDownloadViews();
}

async function cancelDownload(name, button) {
  await act(button, () => api('POST', '/api/models/' + enc(name) + '/cancel'));
}

async function removeModel(m, button) {
  const ok = await ask({ title: t('ask.remove_model_title', { name: modelLabel(m) }), body: t('ask.remove_model_body', { size: fmtBytes(m.size) }), ok: t('stt.remove'), danger: true });
  if (!ok) return;
  const res = await act(button, () => api('DELETE', '/api/models/' + enc(m.name)));
  if (res) { await loadModels(); refreshDownloadViews(); }
}

function onDownload(d) {
  const was = S.downloads[d.name];
  if (d.state === 'running') {
    S.downloads[d.name] = { done: d.done, total: d.total };
    if (was) {
      for (const el of document.querySelectorAll(`[data-dl="${CSS.escape(d.name)}"]`)) updateProgress(el, S.downloads[d.name]);
      return;
    }
  } else {
    delete S.downloads[d.name];
    const m = S.models && S.models.models.find((x) => x.name === d.name);
    const label = m ? modelLabel(m) : d.name;
    if (d.state === 'done' && (!m || m.kind !== 'vad')) toast(t('stt.download_done', { name: label }), 'ok');
    if (d.state === 'error') toast(d.error || '', 'error', t('stt.download_failed', { name: label }));
    if (d.state === 'done' && S.startAfterDownload === d.name) {
      S.startAfterDownload = null;
      // asked for while meetings started by themselves; since set to start by hand, a start now would
      // open a meeting nobody started
      if (!isManual()) listen('start');
    }
    if (S.heldStt && S.heldStt.name === d.name) {
      if (d.state === 'done') applyHeldStt(S.heldStt);
      else {  // failed or cancelled: the current speech-to-text stays, and so does what Settings shows
        S.heldStt = null;
        if (S.route.name === 'settings' && !dirty()) redrawSection();
      }
    }
  }
  loadModels().catch(() => {}).finally(refreshDownloadViews);
}

function refreshDownloadViews() {
  refreshModelLists();
  if (S.setup) renderSetupFoot();
}

// ------------------------------------------------------------------- key fields

const SECRET_LINKS = {
  ANTHROPIC_API_KEY: 'https://console.anthropic.com/settings/keys',
  OPENAI_API_KEY: 'https://platform.openai.com/api-keys',
  DEEPGRAM_API_KEY: 'https://console.deepgram.com/',
  HUBSPOT_TOKEN: 'https://app.hubspot.com/private-apps/',
};

// The last check of each key, {ok, pending, detail}, outside the fields: saving a key publishes
// settings.changed, which redraws the section with new fields while the check is still running.
const KEY_CHECKS = new Map();
const KEY_FIELDS = new Set();  // {name, el, draw} of the key fields drawn, to show a check's result in

function redrawKeyFields(name) {
  for (const f of [...KEY_FIELDS]) {
    if (!f.el.isConnected) { KEY_FIELDS.delete(f); continue; }
    if (f.name === name) f.draw();
  }
}

/** A key or token: "Saved in Keychain ✓" with Replace / Test / Remove, or a field to paste it. */
function keyField(name, { label, help, onChange, testable = true } = {}) {
  const box = h('div', { class: 'keyfield', dataset: { path: 'secret:' + name, secret: name } });
  let editing = false;
  const draw = () => {
    const result = KEY_CHECKS.get(name);
    const isSet = Boolean(S.settings && S.settings.secrets[name]);
    const store = S.app && S.app.capabilities.keychain ? t('key.saved_keychain') : t('key.saved_file');
    const head = h('div', { class: 'head' }, icon('key'), h('strong', { class: 'grow' }, label || name),
      isSet ? h('span', { class: 'pill ok', id: 'ks-' + name }, icon('check'), store) : h('span', { class: 'pill', id: 'ks-' + name }, t('key.not_set')));
    const parts = [head];
    const where = S.app && S.app.capabilities.keychain ? t('key.kept_keychain') : t('key.kept_file');
    parts.push(h('p', { class: 'help small muted' }, help ? help + ' ' + where : where));
    if (!isSet || editing) {
      const input = h('input', { type: 'password', id: 'key-' + name, autocomplete: 'off', spellcheck: 'false', placeholder: t('key.paste'), 'aria-label': t('key.paste_named', { name: label || name }) });
      const save = h('button', { type: 'submit', class: 'btn primary' }, t('key.save'));
      const form = h('form', { class: 'entry', autocomplete: 'off' }, input, save,
        editing && btn(t('common.cancel'), { onclick: () => { editing = false; draw(); } }));
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const value = input.value.trim();
        if (!value) return input.focus();
        act(save, async () => {
          const res = await api('PUT', '/api/secrets/' + enc(name), { value });
          input.value = '';
          S.settings.secrets[name] = res.set;
          editing = false;
          KEY_CHECKS.delete(name);
          draw();
          toast(t('key.saved_toast', { name: label || name }), 'ok');
          if (onChange) onChange(res.set);
          if (testable) test(true);
        });
      });
      parts.push(form);
    } else {
      parts.push(h('div', { class: 'entry' },
        btn(t('key.replace'), { cls: 'small', onclick: () => { editing = true; KEY_CHECKS.delete(name); draw(); box.querySelector('input')?.focus(); } }),
        testable && btn(t('key.test'), { cls: 'small', dataset: { act: 'test' }, onclick: (e) => test(e.currentTarget) }),
        btn(t('key.remove'), { cls: 'small ghost danger', onclick: (e) => remove(e.currentTarget) }, 'trash')));
    }
    if (result) {
      parts.push(h('p', { class: 'result ' + (result.ok ? 'ok' : 'bad'), role: 'status' },
        icon(result.ok ? 'checkCircle' : 'xCircle'), result.pending ? t('key.testing') : humanize(result.detail)));
    }
    fill(box, ...parts);
  };
  const test = async (button) => {
    if (!button) return;
    const pending = { ok: true, pending: true, detail: '' };
    KEY_CHECKS.set(name, pending);
    redrawKeyFields(name);
    const res = await act(null, () => api('POST', '/api/secrets/' + enc(name) + '/test'));
    if (KEY_CHECKS.get(name) !== pending) return;  // replaced or removed meanwhile
    if (res) KEY_CHECKS.set(name, { ok: res.ok, detail: res.detail });
    else KEY_CHECKS.delete(name);
    redrawKeyFields(name);  // the field on the page now, which may not be this one any more
  };
  const remove = async (button) => {
    const ok = await ask({ title: t('key.remove_title', { name: label || name }), body: t('key.remove_body'), ok: t('key.remove'), danger: true });
    if (!ok) return;
    await act(button, async () => {
      const res = await api('PUT', '/api/secrets/' + enc(name), { value: '' });
      S.settings.secrets[name] = res.set;
      KEY_CHECKS.delete(name);
      draw();
      if (onChange) onChange(res.set);
    });
  };
  KEY_FIELDS.add({ name, el: box, draw });
  draw();
  return box;
}

// ------------------------------------------------------------------ permissions

/** "Test microphone" / "Test meeting audio": a one-second capture through the real sources. */
function permissionRow(kind) {
  const box = h('div', { class: 'perm', dataset: { perm: kind } });
  let result = null, busy = false;
  const draw = () => {
    const mic = kind === 'microphone';
    const button = btn(busy ? t('perm.testing') : mic ? t('perm.test_mic') : t('perm.test_system'), {
      cls: 'small', disabled: busy, id: 'perm-' + kind, onclick: run,
    }, busy ? null : mic ? 'mic' : 'speaker');
    if (busy) button.prepend(spinner());
    const parts = [icon(mic ? 'mic' : 'speaker', 'lg'),
      h('div', { class: 'grow' }, h('strong', {}, mic ? t('perm.mic') : t('perm.system')), h('span', {}, mic ? t('perm.mic_desc') : t('perm.system_desc'))),
      button];
    if (result) {
      const status = result.status === 'granted' ? 'ok' : result.status === 'denied' ? 'bad' : 'unknown';
      const text = result.status === 'granted' ? (mic ? t('perm.mic_ok') : t('perm.system_ok'))
        : result.status === 'denied' ? (mic ? t('perm.mic_denied') : t('perm.system_denied'))
        : (mic ? t('perm.mic_unknown') : t('perm.system_unknown'));
      const level = h('span', { class: 'level', 'aria-hidden': 'true' }, h('span'));
      level.firstChild.style.width = result.peak_db != null ? Math.max(4, Math.min(100, ((result.peak_db + 60) / 60) * 100)) + '%' : '0%';
      const fix = result.status !== 'granted' && S.app && S.app.platform === 'darwin'
        && btn(t('fix.open_mac_settings'), { cls: 'small', onclick: () => openPane(mic ? 'microphone' : 'screen_audio') });
      parts.push(h('div', { class: 'result ' + status, role: 'status' },
        icon(status === 'ok' ? 'checkCircle' : status === 'bad' ? 'xCircle' : 'help'),
        // the probe's own words only when ours don't already say it (an error, a denied permission)
        h('span', { class: 'grow' }, text, result.detail && status !== 'ok' && !result.silent ? h('span', { class: 'muted small' }, ' — ' + result.detail) : ''),
        result.status === 'granted' && result.peak_db != null ? level : null, fix));
    }
    fill(box, ...parts);
  };
  async function run() {
    busy = true;
    draw();
    const res = await act(null, () => api('POST', '/api/permissions/' + kind));
    busy = false;
    result = res || null;
    if (S.setup) S.setup.perms[kind] = result;
    draw();
  }
  if (S.setup && S.setup.perms[kind]) result = S.setup.perms[kind];
  draw();
  return box;
}

// -------------------------------------------------------------------- the screen

async function renderSettings() {
  const sec = SECTIONS.includes(S.route.parts[0]) ? S.route.parts[0] : 'general';
  const root = $('screen-settings');
  if (!S.settings || !S.models) {
    fill(root, h('div', { class: 'screen-head' }, h('h1', {}, t('set.title')), h('p', { class: 'sub' }, t('common.loading'))));
    try {
      await Promise.all([S.settings ? null : loadSettings(), S.models ? null : loadModels()]);
    } catch (err) {
      fill(root, h('div', { class: 'screen-head' }, h('h1', {}, t('set.title'))),
        h('div', { class: 'card empty-state' }, icon('alert', 'xl'), h('strong', {}, t('set.error')), humanize(err.message),
          btn(t('action.try_again'), { cls: 'small', id: 'settings-retry', onclick: () => renderSettings() }, 'refresh')));
      return;
    }
  }
  if (S.route.name === 'settings') drawSettings(sec);
}

function currentSection() { return SECTIONS.includes(S.route.parts[0]) ? S.route.parts[0] : 'general'; }

/** ``#/settings/general/starting``: a link to one part of a section (Live's "Change", beside how
 *  meetings start). */
function settingsAnchor() {
  return S.route.name === 'settings' && currentSection() === 'general' && S.route.parts[1] === 'starting' ? 'set-starting' : null;
}

/** That part, scrolled into view with its heading in focus, once the section is drawn (the settings
 *  may still be loading). */
function focusSettingsAnchor() {
  const id = settingsAnchor();
  const el = id && $(id);
  S.anchorPending = Boolean(id) && !el;
  if (!el) return;
  const heading = el.querySelector('h2');
  heading.setAttribute('tabindex', '-1');
  el.scrollIntoView({ block: 'start' });
  heading.focus({ preventScroll: true });
}

function redrawSection() {
  const focused = document.activeElement && document.activeElement.id;
  drawSettings(currentSection());
  if (focused && $(focused)) $(focused).focus({ preventScroll: true });
}

function drawSettings(sec) {
  const root = $('screen-settings');
  const nav = h('nav', { class: 'subnav', 'aria-label': t('set.sections') }, SECTIONS.map((s) =>
    h('a', { href: '#/settings/' + s, 'aria-current': s === sec ? 'page' : null }, t('set.sec.' + s))));
  const builders = { general: generalSection, stt: sttSection, claude: claudeSection, audio: audioSection, tools: toolsSection, agents: agentsSection, advanced: advancedSection };
  const scroll = window.scrollY;
  fill(root, 
    h('div', { class: 'screen-head' }, h('div', {}, h('h1', {}, t('set.title')), h('p', { class: 'sub' }, t('set.sub')))),
    nav,
    h('div', { class: 'settings-body', id: 'settings-body' }, builders[sec]()),
    h('div', { class: 'savebar', id: 'savebar', role: 'region', 'aria-label': t('set.savebar') }));
  window.scrollTo(0, scroll);
  renderSaveBar();
  if (S.anchorPending) focusSettingsAnchor();
}

function renderSaveBar() {
  renderNav();
  const bar = $('savebar');
  if (!bar) return;
  const isDirty = dirty();
  bar.hidden = !isDirty && !S.saveError;
  document.body.classList.toggle('raise-toasts', !bar.hidden);
  const msg = S.saveError
    ? h('p', { class: 'msg error', role: 'alert' }, S.saveError)
    : h('p', { class: 'msg' }, t('set.unsaved'));
  const save = h('button', { type: 'button', class: 'btn primary', id: 'save-settings', disabled: S.saving || !isDirty, onclick: saveSettings },
    S.saving ? spinner() : null, t('set.save'));
  fill(bar, msg, btn(t('set.discard'), { id: 'discard-settings', disabled: S.saving || !isDirty, onclick: discardSettings }), save);
}

function discardSettings() {
  S.draft = {};
  S.prefsDraft = {};
  S.fieldErrors = {};
  S.saveError = null;
  redrawSection();
}

/** Take out of a draft what a save sent: a field typed in again while the save was on its way keeps
 *  its new value, still to be saved. */
function forgetSaved(draft, sent) {
  for (const k of Object.keys(sent)) if (draft[k] === sent[k]) delete draft[k];
}

const STT_KEYS = ['transcription.backend', 'transcription.whisper_cpp.model'];

/** A speech-to-text choice whose model isn't downloaded yet, taken out of ``changes``: sent now, it
 *  would restart listening on a model that can't run — nothing recorded until someone notices. It is
 *  sent once the download is done (:func:`onDownload`); the current speech-to-text keeps working. */
function holdUntilDownloaded(changes) {
  if (!STT_KEYS.some((k) => k in changes)) return null;
  const backend = 'transcription.backend' in changes ? changes['transcription.backend'] : savedVal('transcription.backend');
  const name = 'transcription.whisper_cpp.model' in changes ? changes['transcription.whisper_cpp.model'] : savedVal('transcription.whisper_cpp.model');
  const m = backend === 'whisper-cpp' && S.models && S.models.models.find((x) => x.name === name);
  if (!m || m.installed) return null;
  const held = { name, changes: {} };
  for (const k of STT_KEYS) {
    if (k in changes) { held.changes[k] = changes[k]; delete changes[k]; }
  }
  return held;
}

/** Start downloading a speech model you picked, unless it is there or on its way. */
function fetchModel(name) {
  const m = S.models && S.models.models.find((x) => x.name === name);
  if (m && !m.installed && !S.downloads[name]) downloadModel(name, null);
}

async function applyHeldStt(held) {
  if (S.heldStt !== held) return;
  S.heldStt = null;
  const label = modelLabel(S.models.models.find((x) => x.name === held.name) || { name: held.name, label: held.name });
  const res = await act(null, () => api('PATCH', '/api/settings', { changes: held.changes }));
  if (!res) return;
  await Promise.all([loadSettings(), loadModels()]).catch(() => {});
  toast(res.restart_required ? t('stt.switched_later', { name: label }) : t('stt.switched', { name: label }), 'ok');
  if (S.route.name === 'settings' && !dirty()) redrawSection();
}

async function saveSettings() {
  // a value already shown as wrong under its field (minutes that aren't a whole number…): fix it first
  const wrong = Object.keys(S.fieldErrors).find((p) => p.startsWith('pref:'));
  if (wrong) return showSaveError(wrong, S.fieldErrors[wrong]);
  // the meeting ends by itself only after "Still in a meeting?" is asked: the end is checked first, so
  // an end at or before the question would end it without ever asking (0 is "never", for either)
  const ask = pref('silence_ask_minutes'), end = pref('silence_end_minutes');
  if (pref('meeting_start') !== 'automatic' && ask > 0 && end > 0 && ask >= end) {
    return showSaveError('pref:silence_end_minutes', t('err.end_before_ask', { n: fmtNum(ask) }));
  }
  const changes = { ...S.draft };
  const prefs = { ...S.prefsDraft };
  const sent = { ...changes };  // as typed, before the hold below takes a model still to download out
  const held = holdUntilDownloaded(changes);
  S.saving = true;
  S.saveError = null;
  renderSaveBar();
  let res = null;
  const sendPrefs = async () => {
    if (!Object.keys(prefs).length) return;
    const saved = await api('PATCH', '/api/prefs', prefs).catch((err) => {
      // "silence_ask_minutes: Input should be…": under the preference's own field
      const m = err.status === 400 && /^(\w+): (.*)$/s.exec(err.message || '');
      if (m && m[1] in prefs) err.body = { field: 'pref:' + m[1], error: m[2] };
      throw err;
    });
    forgetSaved(S.prefsDraft, prefs);
    setApp({ ...S.app, prefs: { ...S.app.prefs, ...saved }, mode: saved.meeting_start || S.app.mode });
    if (saved.meeting_start && saved.meeting_start !== 'automatic') S.startAfterDownload = null;  // by hand from now on
  };
  const sendSettings = async () => {
    if (Object.keys(changes).length) res = await api('PATCH', '/api/settings', { changes });
    forgetSaved(S.draft, sent);
  };
  // switched to by hand, the preferences go first: listening all the time stops before the settings
  // arrive, which would otherwise restart it (or retry a failed start) in automatic mode — the
  // microphone open after "by hand" was asked for, and a restart that opens a meeting nobody
  // started. Any other save sends the settings first, as it always did: a start that failed by hand
  // is only checked again then, so a switch to automatic saved with its fix starts nothing by itself,
  // as from a clean stop. One refused, what comes after it isn't sent and stays on screen.
  const toManual = 'meeting_start' in prefs && prefs.meeting_start !== 'automatic' && S.app.prefs.meeting_start === 'automatic';
  try {
    for (const send of toManual ? [sendPrefs, sendSettings] : [sendSettings, sendPrefs]) await send();
    S.fieldErrors = {};
    await Promise.all([loadSettings(), loadModels()]);
  } catch (err) {
    S.saving = false;
    if (err.status === 400 && err.body && err.body.field !== undefined) return showSaveError(err.body.field, err.body.error);
    S.saveError = humanize(err.message);
    renderSaveBar();
    return;
  }
  S.saving = false;
  let msg = res && res.restart_required ? t('set.saved_later') : res && res.restarting ? t('set.saved_restarting') : t('set.saved');
  if (held) {
    S.heldStt = held;
    const m = S.models.models.find((x) => x.name === held.name);
    if (m.installed) {
      applyHeldStt(held);  // it finished downloading while this was being saved
    } else {
      fetchModel(held.name);
      msg = t('set.saved_after_download', { name: modelLabel(m) });
    }
  }
  toast(msg, 'ok', t('set.saved_title'));
  if (S.route.name === 'settings') redrawSection();
  if (S.route.name === 'live') renderLive();
  // Reminders just turned on: check it now, so macOS asks for the permission while you are here
  if (changes['sinks.reminders.enabled'] === true && S.route.name === 'settings' && $('test-reminders')) {
    testTool('reminders', $('test-btn-reminders'), $('test-reminders'));
  }
}

/** A refused save: open the section with the field, show the message under it, focus it. */
function showSaveError(field, message) {
  S.fieldErrors = {};
  const path = field || '';
  const sec = path ? sectionFor(path) : currentSection();
  if (sec !== currentSection()) {
    history.replaceState(null, '', '#/settings/' + sec);
    S.route = parseRoute();
  }
  drawSettings(sec);
  let target = null, best = -1;
  for (const el of document.querySelectorAll('#settings-body [data-path]')) {
    const p = el.dataset.path;
    if ((path === p || path.startsWith(p + '.')) && p.length > best) { target = el; best = p.length; }
  }
  if (target) {
    S.fieldErrors[target.dataset.path] = message;
    drawSettings(sec);
    const again = document.querySelector(`#settings-body [data-path="${CSS.escape(target.dataset.path)}"]`);
    if (again) {
      again.scrollIntoView({ block: 'center' });
      const input = again.querySelector('input:not([type=checkbox]), select, textarea') || again.querySelector('input');
      if (input) input.focus({ preventScroll: true });
    }
    S.saveError = t('set.fix_below');
  } else {
    S.saveError = friendlyError(path, message);
  }
  renderSaveBar();
}

/** One settings section as a row: what it is about on the left, its controls in a panel on the right.
 *  ``lead`` is its sentence, or ``[sentence, ...more]`` with what goes under it (a summary of what is on). */
function card(title, lead, ...body) {
  const [text, ...more] = [].concat(lead || []);
  return h('section', { class: 'set-row' },
    h('div', { class: 'set-label' }, h('h2', {}, title), text && h('p', { class: 'lead' }, text), more),
    h('div', { class: 'card set-panel' }, ...body));
}

// ---------------------------------------------------------------------- sections

function generalSection() {
  const caps = S.app.capabilities || {};
  const loginHelp = caps.login_item ? t('set.login_help') : t('set.login_unavailable');
  return h('div', {},
    startingSection(),
    card(t('set.you'), t('set.you_lead'),
      field('user_name', t('set.name'), textInput('user_name', { placeholder: t('set.name_ph') }), { help: t('set.name_help') }),
      h('div', { class: 'field', dataset: { path: 'transcription.language' } },
        h('span', { class: 'label', id: 'lang-label' }, t('set.languages')),
        languageCards(t('set.languages'), langChoice(), (choice) => {
          for (const [p, v] of Object.entries(LANG_CHOICES[choice] || {})) setDraft(p, v);
          redrawSection();
        }),
        errorEl('transcription.language'))),
    card(t('set.app'), t('set.app_lead'),
      h('div', { class: 'field', dataset: { path: 'pref:ui_language' } },
        h('label', { for: 'p-ui_language' }, t('set.ui_language')),
        h('div', { class: 'control' }, uiLanguageSelect())),
      prefSwitch('start_at_login', t('set.start_at_login'), loginHelp, { disabled: !caps.login_item })));
}

/** Settings → General → Starting meetings: when Voicestream records (by hand or by itself) and every
 *  way of starting a meeting from any app. Saved with Save, as the rest of the screen (the shortcut
 *  at once, as macOS's own). What only the Mac app does is said, and its switches are off elsewhere. */
function startingSection() {
  const caps = S.app.capabilities || {};
  const manual = pref('meeting_start') !== 'automatic';
  const mac = (cap, key, label, help) => prefSwitch(key, label, caps[cap] ? help : help + ' ' + t('set.login_unavailable'), { disabled: !caps[cap] });
  const section = card(t('start.title'), [t('start.lead'), startingSummary()],
    h('div', { class: 'field', dataset: { path: 'pref:meeting_start' } },
      h('span', { class: 'label', id: 'start-mode-label' }, t('start.how')),
      choiceCards(t('start.how'), [
        { value: 'manual', title: t('start.manual'), desc: t('start.manual_desc') },
        { value: 'automatic', title: t('start.automatic'), desc: t('start.automatic_desc') },
      ], manual ? 'manual' : 'automatic', (v) => {
        setPref('meeting_start', v);
        // automatically, the silence fields are not on screen: what was typed wrong in them is dropped
        // (prefMinutes keeps it as typed, a string), or Save would fail on fields nobody can see
        if (v === 'automatic') {
          for (const k of SILENCES) {
            if (typeof S.prefsDraft[k] !== 'number') delete S.prefsDraft[k];
            delete S.fieldErrors['pref:' + k];
          }
        }
        redrawSection();
      }, { cols: 2 })),
    manual && prefMinutes('silence_ask_minutes', t('start.ask'), t('start.ask_help')),
    manual && prefMinutes('silence_end_minutes', t('start.end'), t('start.end_help')),
    !manual && field('always_on.idle_split_minutes', t('set.idle'), numberInput('always_on.idle_split_minutes', { min: 0, max: 240, step: 1 }),
      { help: t('set.idle_help'), extra: h('span', { class: 'suffix' }, t('set.minutes')) }),
    !manual && prefSwitch('listen_on_launch', t('set.listen_on_launch'), t('set.listen_on_launch_help')),
    shortcutSetting(),
    mac('floating_button', 'floating_button', t('start.floating'), t('start.floating_help')),
    mac('spotlight', 'spotlight_launcher', t('start.spotlight'), t('start.spotlight_help')),
    mac('call_detection', 'call_suggestions', t('start.suggest'), t('start.suggest_help')));
  section.id = 'set-starting';
  return section;
}

/** Beside the section, how meetings start now (as saved): the way, the shortcut and what is on. */
function startingSummary() {
  const a = S.app, p = a.prefs, caps = a.capabilities || {};
  const on = [p.meeting_start === 'automatic' ? t('start.summary_auto') : t('start.summary_manual')];
  if (p.hotkey && caps.hotkey) on.push(t('start.summary_keys', { keys: shortcutText(p.hotkey) }));
  if (p.floating_button && caps.floating_button) on.push(t('start.summary_floating'));
  if (p.spotlight_launcher && caps.spotlight) on.push(t('start.summary_spotlight'));
  if (p.call_suggestions && caps.call_detection) on.push(t('start.summary_suggest'));
  return h('div', { class: 'summary', id: 'start-summary' }, t('start.summary'), on.map((text) => h('span', {}, icon('check'), text)));
}

const SILENCES = ['silence_ask_minutes', 'silence_end_minutes'];

/** A number of minutes for a preference (0 to 600; 0: never), checked as you type: a value the app
 *  would refuse says so under the field, and Save says to fix it first. */
function prefMinutes(key, label, help) {
  const path = 'pref:' + key;
  const el = h('input', { type: 'number', id: 'p-' + key, class: 'narrow', value: String(pref(key)), min: 0, max: 600, step: 1,
    inputmode: 'numeric', autocomplete: 'off' });
  el.addEventListener('input', () => {
    const raw = el.value.trim();
    const n = Number(raw);
    if (raw !== '' && Number.isInteger(n) && n >= 0 && n <= 600) {
      delete S.fieldErrors[path];
      markField(path, null);
      // the other one's message was about the two together ("ask before the end"), unless its own value is wrong
      const other = SILENCES.find((k) => k !== key);
      if (typeof S.prefsDraft[other] !== 'string' && S.fieldErrors['pref:' + other]) {
        delete S.fieldErrors['pref:' + other];
        markField('pref:' + other, null);
      }
      setPref(key, n);
    } else {
      S.fieldErrors[path] = t('err.minutes');
      markField(path, S.fieldErrors[path]);
      S.prefsDraft[key] = raw;  // unsaved, and not sendable as it is
      renderSaveBar();
    }
  });
  return field(path, label, el, { help, extra: h('span', { class: 'suffix' }, t('start.minutes_silence')) });
}

function uiLanguageSelect() {
  const options = [['auto', t('set.ui_auto')], ['en', 'English'], ['fr', 'Français']];
  const el = h('select', { id: 'p-ui_language' }, options.map(([v, label]) => h('option', { value: v, selected: pref('ui_language') === v || null }, label)));
  el.addEventListener('change', () => setPref('ui_language', el.value));
  return el;
}

function sttPrice(model) {
  const per = S.settings.values.pricing.stt_per_minute[model];
  return per ? money(per * 60, 2) : null;
}

function sttSection() {
  const backend = val('transcription.backend');
  const tc = S.settings.values.transcription;
  const options = [
    { value: 'whisper-cpp', title: t('stt.local'), desc: t('stt.local_desc') },
    { value: 'openai', title: t('stt.openai'), desc: t('stt.cloud_desc', { price: sttPrice(tc.openai.model) || '—' }) },
    { value: 'deepgram', title: t('stt.deepgram'), desc: t('stt.cloud_desc', { price: sttPrice(tc.deepgram.model) || '—' }) },
  ];
  if (!options.some((o) => o.value === backend)) options.push({ value: backend, title: t('stt.other', { name: backend }), desc: t('stt.other_desc') });
  let detail;
  if (backend === 'whisper-cpp') {
    const held = S.heldStt && S.models && S.models.models.find((m) => m.name === S.heldStt.name);
    detail = h('div', { class: 'sub-card' },
      h('h3', {}, t('stt.models')), h('p', { class: 'help small muted' }, t('stt.models_help')),
      held && h('p', { class: 'help small', id: 'stt-held', role: 'status' }, icon('download'), ' ', t('stt.switch_after', { name: modelLabel(held) })),
      // picking one that isn't here downloads it; it is used once it is there (see holdUntilDownloaded)
      modelList({ pick: (name) => { setDraft('transcription.whisper_cpp.model', name); fetchModel(name); redrawSection(); } }),
      S.models && S.models.models_dir && h('p', { class: 'small faint' }, t('stt.models_dir', { dir: S.models.models_dir })));
  } else if (backend === 'openai' || backend === 'openai-realtime') {
    detail = h('div', { class: 'sub-card' }, keyField('OPENAI_API_KEY', { label: t('key.openai'), help: t('key.openai_help') }),
      h('p', { class: 'small' }, h('a', { href: SECRET_LINKS.OPENAI_API_KEY, target: '_blank', rel: 'noopener noreferrer' }, t('key.where_openai'), ' ', icon('external'))));
  } else if (backend === 'deepgram') {
    detail = h('div', { class: 'sub-card' }, keyField('DEEPGRAM_API_KEY', { label: t('key.deepgram'), help: t('key.deepgram_help') }),
      h('p', { class: 'small' }, h('a', { href: SECRET_LINKS.DEEPGRAM_API_KEY, target: '_blank', rel: 'noopener noreferrer' }, t('key.where_deepgram'), ' ', icon('external'))));
  }
  return h('div', {},
    card(t('stt.title'), [t('stt.lead'), sttSummary()],
      h('div', { class: 'field', dataset: { path: 'transcription.backend' } },
        choiceCards(t('stt.title'), options, backend, (v) => { setDraft('transcription.backend', v); redrawSection(); }),
        errorEl('transcription.backend')),
      detail));
}

/** Beside the choices, what turns the voices into text now (as saved), as Your tools lists what is on. */
function sttSummary() {
  const backend = savedVal('transcription.backend');
  const engine = { 'whisper-cpp': 'stt.local', openai: 'stt.openai', 'openai-realtime': 'stt.openai', deepgram: 'stt.deepgram' }[backend];
  let detail = null;
  if (backend === 'whisper-cpp') {
    const m = S.models && S.models.models.find((x) => x.name === savedVal('transcription.whisper_cpp.model'));
    if (m) detail = t('stt.summary_model', { name: modelLabel(m), size: fmtBytes(m.size) });
  } else if (backend === 'openai' || backend === 'deepgram') {
    const price = sttPrice(savedVal(`transcription.${backend}.model`));
    if (price) detail = t('stt.summary_price', { price });
  }
  return h('div', { class: 'summary', id: 'stt-summary' }, t('stt.summary'),
    h('span', {}, icon('check'), engine ? t(engine) : t('stt.other', { name: backend })), detail);
}

function claudeSection() {
  const provider = val('brain.provider');
  const models = [['claude-haiku-5-5', t('claude.model_haiku')], ['claude-sonnet-5-5', t('claude.model_sonnet')]];
  return h('div', {},
    card(t('claude.title'), t('claude.lead'),
      keyField('ANTHROPIC_API_KEY', { label: t('key.claude'), help: t('key.claude_help') }),
      h('p', { class: 'small' }, h('a', { href: SECRET_LINKS.ANTHROPIC_API_KEY, target: '_blank', rel: 'noopener noreferrer' }, t('key.where_claude'), ' ', icon('external'))),
      settingSwitch('brain.provider', t('claude.use'), t('claude.use_help'), { on: 'anthropic', off: 'heuristic', redraw: true }),
      provider === 'anthropic' && field('brain.model', t('claude.model'), selectInput('brain.model', models), { help: t('claude.model_help') })),
    card(t('claude.budget_title'), t('claude.budget_lead'),
      field('budget.daily_usd', t('claude.budget'), numberInput('budget.daily_usd', { min: 0, step: 0.5 }),
        { help: t('claude.budget_help'), extra: h('span', { class: 'suffix' }, t('claude.per_day')) }),
      h('p', { class: 'small' }, h('a', { href: '#/costs' }, t('claude.see_costs')))));
}

function audioSection() {
  if (!S.devices) {
    api('GET', '/api/devices').then((d) => { S.devices = d; if (S.route.name === 'settings' && currentSection() === 'audio') redrawSection(); })
      .catch((err) => { S.devices = { inputs: [], error: err.message, system_audio: null }; if (currentSection() === 'audio') redrawSection(); });
  }
  const dev = S.devices;
  const inputs = dev ? dev.inputs : [];
  const micOptions = [[null, t('audio.default_mic')], ...inputs.map((d) => [d.name, d.name + (d.default ? ' ' + t('audio.default_suffix') : '')])];
  const sources = val('capture.sources') || [];
  const sys = dev && dev.system_audio;
  return h('div', {},
    card(t('audio.title'), t('audio.lead'),
      field('capture.mic.device', t('audio.mic'), dev ? selectInput('capture.mic.device', micOptions) : h('span', { class: 'muted' }, t('common.loading')),
        { help: dev && dev.error ? t('audio.no_devices', { detail: dev.error }) : t('audio.mic_help') }),
      val('capture.mic.device') != null && settingSwitch('capture.mic.use_default_when_missing', t('audio.mic_fallback'), t('audio.mic_fallback_help')),
      switchRow('f-capture_sources', t('audio.system'), t('audio.system_help'), sources.includes('system'), (on) => {
        const base = sources.filter((s) => s !== 'system');
        setDraft('capture.sources', on ? [...base, 'system'] : base);
      }, { path: 'capture.sources' }),
      sys && h('p', { class: 'help small muted' }, icon('info'), ' ', sys.available ? t('audio.system_ok', { detail: sys.detail }) : t('audio.system_missing', { detail: sys.detail })),
      settingSwitch('capture.store_audio', t('audio.store'), t('audio.store_help'))),
    card(t('perm.title'), t('perm.lead'), permissionRow('microphone'), permissionRow('system-audio')));
}

function toolsSection() {
  const catalog = (S.settings.catalog && S.settings.catalog.sinks) || [];
  const others = catalog.map((c) => c.name).filter((n) => !MAIN_TOOLS.includes(n));
  const more = h('details', { class: 'card more-tools', open: others.some((n) => val(`sinks.${n}.enabled`)) || null },
    h('summary', { class: 'card-pad' }, h('strong', {}, t('tools.more')), ' ', h('span', { class: 'muted small' }, t('tools.more_help'))),
    h('div', { class: 'card-pad' }, others.map((n) => toolCard(n, true))));
  return h('div', {},
    h('section', { class: 'set-row' },
      h('div', { class: 'set-label' }, h('h2', {}, t('set.sec.tools')), h('p', { class: 'lead' }, t('tools.lead')),
        toolsSummary(catalog)),
      h('div', { class: 'tool-list' },
        ...MAIN_TOOLS.filter((n) => catalog.some((c) => c.name === n)).map((n) => toolCard(n)),
        others.length ? more : null)));
}

/** Beside the tools, the ones that are on (as saved), so the list says what it adds up to. */
function toolsSummary(catalog) {
  const on = catalog.map((c) => c.name).filter((n) => S.settings.sinks[n] && S.settings.sinks[n].enabled);
  return h('div', { class: 'summary' }, on.length ? t('tools.summary') : t('tools.summary_none'),
    on.map((n) => h('span', {}, icon('check'), has('tool.' + n + '.name') ? t('tool.' + n + '.name') : n)));
}

function toolCard(name, compact = false) {
  const cat = ((S.settings.catalog && S.settings.catalog.sinks) || []).find((c) => c.name === name) || { name, label: name, secrets: [] };
  const sk = S.settings.sinks[name] || { enabled: false, values: {} };
  const enabled = Boolean(val(`sinks.${name}.enabled`));
  const path = 'sinks.' + name;
  const err = S.fieldErrors[path];
  const mac = S.app.platform === 'darwin';
  const title = has('tool.' + name + '.name') ? t('tool.' + name + '.name') : cat.label;
  const desc = has('tool.' + name + '.desc') ? t('tool.' + name + '.desc') : '';
  const sw = h('input', { type: 'checkbox', role: 'switch', class: 'switch', id: fid(path + '.enabled'), checked: enabled, 'aria-label': t('tools.enable', { name: title }), disabled: cat.available === false });
  sw.addEventListener('change', () => { setDraft(path + '.enabled', sw.checked); redrawSection(); });
  const testOut = h('p', { class: 'small', role: 'status', id: 'test-' + name });
  const body = enabled ? h('div', { class: 'tool-body' },
    ...toolFields(name, cat),
    sk.error && !err && h('p', { class: 'error-text' }, friendlyError(path, sk.error)),
    h('div', { class: 'row' }, btn(t('tools.test'), { cls: 'small', id: 'test-btn-' + name, onclick: (e) => testTool(name, e.currentTarget, testOut) }), testOut)) : null;
  return h('section', { class: 'card tool' + (err ? ' invalid' : ''), dataset: { path, tool: name } },
    h('div', { class: 'tool-head' },
      h('div', { class: 'grow' }, h('strong', {}, title,
        cat.mac_only && !mac ? h('span', { class: 'pill' }, ' ', t('tools.mac_only')) : null),
      desc && h('span', {}, desc),
      cat.available === false && h('span', { class: 'error-text' }, cat.error || '')),
      sw),
    h('p', { class: 'field-error tool-error', role: err ? 'alert' : null }, err ? friendlyError(path, err) : ''),
    body);
}

function toolFields(name, cat) {
  const p = (k) => `sinks.${name}.${k}`;
  switch (name) {
    case 'reminders':
      return [
        field(p('list_name'), t('tools.reminders.list'), textInput(p('list_name'), { nullIfEmpty: false }), { help: t('tools.reminders.list_help') }),
        settingSwitch(p('owners'), t('tools.only_mine'), t('tools.only_mine_help'), { on: 'mine', off: 'all' }),
        field(p('due_time'), t('tools.reminders.time'), textInput(p('due_time'), { placeholder: '09:00', cls: 'narrow' }), { help: t('tools.reminders.time_help') }),
      ];
    case 'markdown':
      return [
        field(p('todo_file'), t('tools.markdown.todo'), textInput(p('todo_file'), { placeholder: t('tools.markdown.todo_ph') }), { help: t('tools.markdown.todo_help') }),
        field(p('notes_dir'), t('tools.markdown.notes'), textInput(p('notes_dir'), { placeholder: t('tools.markdown.notes_ph') }), { help: t('tools.markdown.notes_help') }),
      ];
    case 'google':
      return [googleSteps(),
        keyField('VOICESTREAM_GOOGLE_URL', { label: t('tools.google.url'), help: t('tools.google.url_help'), testable: false }),
        keyField('VOICESTREAM_GOOGLE_SECRET', { label: t('tools.google.secret'), help: t('tools.google.secret_help'), testable: false }),
        settingSwitch(p('calendar'), t('tools.google.calendar'), t('tools.google.calendar_help')),
        settingSwitch(p('docs'), t('tools.google.docs'), t('tools.google.docs_help')),
        settingSwitch(p('recap_to_self'), t('tools.google.recap'), t('tools.google.recap_help')),
        settingSwitch(p('followup_draft'), t('tools.google.draft'), t('tools.google.draft_help')),
      ];
    case 'hubspot':
      return [
        keyField('HUBSPOT_TOKEN', { label: t('tools.hubspot.token'), help: t('tools.hubspot.token_help') }),
        field(p('owner_email'), t('tools.hubspot.owner'), textInput(p('owner_email'), { type: 'email', placeholder: 'you@company.com' }), { help: t('tools.hubspot.owner_help') }),
        settingSwitch(p('log_meeting'), t('tools.hubspot.log'), t('tools.hubspot.log_help')),
      ];
    case 'shortcuts':
      return [
        h('p', { class: 'help small muted' }, t('tools.shortcuts.lead')),
        field(p('item_shortcut'), t('tools.shortcuts.item'), textInput(p('item_shortcut'), { placeholder: t('tools.shortcuts.item_ph') })),
        field(p('notes_shortcut'), t('tools.shortcuts.notes'), textInput(p('notes_shortcut'))),
        field(p('update_shortcut'), t('tools.shortcuts.update'), textInput(p('update_shortcut'))),
      ];
    case 'command':
      return [
        h('p', { class: 'help small muted' }, t('tools.command.lead')),
        field(p('item_command'), t('tools.command.item'), textInput(p('item_command'), { cls: 'mono', placeholder: 'open "promethee://add"' })),
        field(p('notes_command'), t('tools.command.notes'), textInput(p('notes_command'), { cls: 'mono' })),
      ];
    default:
      return [
        ...(cat.secrets || []).map((s) => keyField(s, { label: s })),
        h('p', { class: 'help small muted' }, t('tools.other_help'), ' ',
          btn(t('adv.open_config'), { cls: 'link', onclick: () => reveal('config') })),
      ];
  }
}

function googleSteps() {
  const copy = btn(t('tools.google.copy_script'), { cls: 'small', onclick: (e) => act(e.currentTarget, async () => {
    const res = await api('GET', '/api/google-bridge');
    await copyText(res.script);
    toast(t('tools.google.copied'), 'ok');
  }) }, 'copy');
  const secret = btn(t('tools.google.make_secret'), { cls: 'small', onclick: (e) => act(e.currentTarget, async () => {
    const bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    const value = btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, (c) => ({ '+': '-', '/': '_', '=': '' })[c]);
    await copyText(value);
    await api('PUT', '/api/secrets/VOICESTREAM_GOOGLE_SECRET', { value });
    S.settings.secrets.VOICESTREAM_GOOGLE_SECRET = true;
    toast(t('tools.google.secret_made'), 'ok');
    redrawSection();
  }) }, 'key');
  return h('div', { class: 'sub-card' },
    h('h3', {}, t('tools.google.steps_title')),
    h('ol', { class: 'steps' },
      h('li', {}, t('tools.google.step1'), ' ', h('a', { href: 'https://script.google.com/home/projects/create', target: '_blank', rel: 'noopener noreferrer' }, 'script.google.com', icon('external'))),
      h('li', {}, t('tools.google.step2'), ' ', copy),
      h('li', {}, t('tools.google.step3'), ' ', secret),
      h('li', {}, t('tools.google.step4')),
      h('li', {}, t('tools.google.step5')),
      h('li', {}, t('tools.google.step6'))));
}

/** A tool's own check as saved, ``{status, detail}`` (``null``: it is off). GET /api/doctor runs
 *  every enabled tool's; for Apple Reminders that is when macOS asks whether Voicestream may
 *  control Reminders — better now, while you are here, than at a meeting's unattended wrap-up,
 *  where the to-dos would wait on the question until they time out. */
async function checkTool(name) {
  const res = await api('GET', '/api/doctor');
  return res.checks.find((c) => c.id === 'sink:' + name) || null;
}

/** A check's result as a line: ✓ Works… or ✗ the problem, with its fix when there is one. */
function toolCheckLine(check) {
  const ok = check.status === 'ok';
  const fix = !ok && fixFor(check.detail);
  return [icon(ok ? 'checkCircle' : 'xCircle'), ' ', ok ? t('tools.test_ok', { detail: check.detail }) : humanize(check.detail),
    fix ? h('span', {}, ' ', btn(fix.label, { cls: 'small', onclick: fix.run })) : null];
}

async function testTool(name, button, out) {
  if (dirty()) {
    out.className = 'small error-text';
    out.textContent = t('tools.save_first');
    return;
  }
  out.className = 'small muted';
  fill(out, spinner(), ' ', name === 'reminders' ? t('tools.reminders.asking') : t('tools.testing'));
  const check = await act(button, () => checkTool(name));
  if (check === undefined) { out.textContent = ''; return; }
  if (!check) {
    out.className = 'small muted';
    out.textContent = t('tools.test_off');
  } else {
    out.className = 'small ' + (check.status === 'ok' ? 'ok-text' : 'error-text');
    fill(out, ...toolCheckLine(check));
  }
}

function agentsSection() {
  const mode = val('agents.mode');
  const options = ['off', 'suggest', 'commands', 'auto'].map((value) => ({ value, title: t('agents.' + value), desc: t('agents.' + value + '.desc') }));
  return h('div', {},
    card(t('agents.title'), t('agents.lead'),
      h('div', { class: 'field', dataset: { path: 'agents.mode' } },
        choiceCards(t('agents.title'), options, mode, (v) => { setDraft('agents.mode', v); redrawSection(); }, { list: true }),
        errorEl('agents.mode')),
      mode !== 'off' && field('budget.agents_daily_usd', t('agents.daily'), numberInput('budget.agents_daily_usd', { min: 0, step: 0.5 }),
        { help: t('agents.daily_help'), extra: h('span', { class: 'suffix' }, t('claude.per_day')) }),
      mode !== 'off' && field('agents.max_budget_usd', t('agents.per_task'), numberInput('agents.max_budget_usd', { min: 0, step: 0.25 }),
        { help: t('agents.per_task_help'), extra: h('span', { class: 'suffix' }, t('agents.per_task_suffix')) })));
}

function advancedSection() {
  const a = S.app;
  const checks = h('div', { class: 'checks', id: 'doctor' });
  const run = btn(t('adv.check'), { id: 'run-doctor', onclick: (e) => act(e.currentTarget, async () => {
    fill(checks, h('p', { class: 'muted' }, spinner(), ' ', t('adv.checking')));
    const res = await api('GET', '/api/doctor');
    fill(checks, ...res.checks.map((c) => h('div', { class: 'check ' + c.status },
      icon(c.status === 'ok' ? 'checkCircle' : c.status === 'warn' ? 'alert' : 'xCircle'),
      h('div', { class: 'grow' }, h('div', {}, c.title), c.detail && h('div', { class: 'detail' }, c.detail), c.fix && h('div', { class: 'detail' }, '→ ' + c.fix)))));
  }) }, 'checkCircle');
  return h('div', {},
    card(t('adv.files'), t('adv.files_lead'),
      h('dl', { class: 'kv' },
        h('dt', {}, t('adv.config')), h('dd', { class: 'mono' }, a.config_path),
        h('dt', {}, t('adv.data')), h('dd', { class: 'mono' }, a.data_dir),
        h('dt', {}, t('adv.version')), h('dd', {}, a.version + ' · ' + a.platform)),
      h('div', { class: 'row' },
        btn(t('adv.open_config'), { onclick: () => reveal('config') }, 'folder'),
        btn(t('adv.open_data'), { onclick: () => reveal('data_dir') }, 'folder'),
        btn(t('adv.open_log'), { onclick: () => reveal('log') }, 'folder'))),
    card(t('adv.health'), t('adv.health_lead'), h('div', { class: 'row' }, run), checks),
    card(t('adv.setup'), t('adv.setup_lead'), btn(t('adv.setup_again'), { id: 'setup-again', onclick: () => openSetup(true) }, 'refresh')));
}

function onSettingsChanged() {
  // our own saves reload by themselves; a change made elsewhere (another window) shows up once nothing is pending
  if (dirty() || S.saving) return;
  Promise.all([loadSettings(), loadModels()]).then(() => {
    if (S.route.name === 'settings' && !dirty()) redrawSection();
  }).catch(() => {});
}

// ==================================================================== setup wizard

const STEPS = ['welcome', 'you', 'stt', 'claude', 'perms', 'tools', 'start', 'done'];
const STEP_KEY = 'voicestream-setup-step';

function recommendedModel() {
  const m = S.models && S.models.models.find((x) => x.recommended);
  return m ? m.name : 'large-v3-turbo-q5_0';
}

/** The first-run wizard (full window). ``forced``: opened again from Settings → Advanced. */
function openSetup(forced = false) {
  if (S.setup) return;
  S.setup = { forced, step: 0, perms: {}, error: null, busy: false, dlStarted: false, choice: null, shown: -1 };
  if (!forced) {
    try {
      const saved = Number(sessionStorage.getItem(STEP_KEY));
      if (saved > 0 && saved < STEPS.length) S.setup.step = saved;
    } catch (_) { /* no storage: start at the beginning */ }
  }
  // a link to one of its steps (#/setup/start) opens it there, as a reload does
  const linked = STEPS.indexOf((/^#\/setup\/(\w+)$/.exec(location.hash) || [])[1]);
  if (linked > 0) S.setup.step = linked;
  $('shell').hidden = true;
  homeNotices();  // not emptied with Live's meeting
  for (const name of SCREENS) $('screen-' + name).replaceChildren();  // drawn again when the setup closes
  $('setup').hidden = false;
  document.body.classList.add('in-setup');
  document.title = t('setup.title') + ' · Voicestream';
  renderSetup();
  // enterStep: reopened on the speech-to-text step (a reload), the model download starts there too
  Promise.all([S.settings ? null : loadSettings(), S.models ? null : loadModels()])
    .then(() => { if (S.setup) { initChoices(); renderSetup(); enterStep(); } })
    .catch((err) => { if (S.setup) { S.setup.error = humanize(err.message); renderSetup(); } });
  if (S.settings && S.models) { initChoices(); renderSetup(); enterStep(); }
}

const CLOUD_STT_KEYS = { openai: 'OPENAI_API_KEY', deepgram: 'DEEPGRAM_API_KEY' };

/** The speech-to-text the wizard starts on: on this Mac (free, private), unless this computer can't
 *  run it, or you really set up a cloud service before — its key is saved too. A new install's
 *  settings say OpenAI without anyone choosing it (the built-in default, and the file the first
 *  save writes from the template). */
function initialStt(backend) {
  const caps = S.app.capabilities || {};
  const key = CLOUD_STT_KEYS[backend];
  if (key && S.settings.config_exists && S.settings.secrets[key]) return backend;
  if (caps.local_stt === false) return key ? backend : 'openai';
  return 'whisper-cpp';
}

function initChoices() {
  const W = S.setup;
  if (W.choice) return;
  const v = S.settings.values;
  const lang = langChoice(savedVal);
  const backend = v.transcription.backend;
  const mac = S.app.platform === 'darwin';
  W.choice = {
    name: v.user_name && v.user_name !== 'Me' ? v.user_name : '',
    lang: lang === 'any' ? 'fr+en' : lang,
    stt: initialStt(backend),
    reminders: S.settings.sinks.reminders ? (S.settings.sinks.reminders.enabled || mac) : mac,
    list: (v.sinks.reminders || {}).list_name || t('setup.tools.list_default'),  // the raw setting: the sink's default is English
    startAtLogin: S.app.prefs.start_at_login,
    listenOnLaunch: S.app.prefs.listen_on_launch,
    // how meetings start, as saved (a new install's defaults: by hand, with every way of starting one on)
    mode: S.app.prefs.meeting_start === 'automatic' ? 'automatic' : 'manual',
    floating: S.app.prefs.floating_button !== false,
    spotlight: S.app.prefs.spotlight_launcher !== false,
    suggestions: S.app.prefs.call_suggestions !== false,
  };
}

function closeSetup() {
  S.setup = null;
  try { sessionStorage.removeItem(STEP_KEY); } catch (_) { /* nothing to forget */ }
  $('setup').hidden = true;
  fill($('setup'));
  document.body.classList.remove('in-setup');
  $('shell').hidden = false;
  navigate(true);
}

function setStep(i) {
  const W = S.setup;
  W.step = Math.max(0, Math.min(STEPS.length - 1, i));
  W.error = null;
  try { sessionStorage.setItem(STEP_KEY, String(W.step)); } catch (_) { /* fine */ }
  renderSetup();
  enterStep();
}

/** Side effects of arriving on a step: the speech model starts downloading as soon as on-device is chosen. */
function enterStep() {
  const W = S.setup;
  if (!W || !W.choice) return;
  if (STEPS[W.step] === 'stt' && W.choice.stt === 'whisper-cpp') startRecommendedDownload();
}

function startRecommendedDownload() {
  const W = S.setup;
  const name = recommendedModel();
  const m = S.models && S.models.models.find((x) => x.name === name);
  if (!m || m.installed || S.downloads[name] || W.dlStarted) return;
  W.dlStarted = true;
  downloadModel(name, null);
}

function renderSetup() {
  const W = S.setup;
  if (!W) return;
  const root = $('setup');
  const step = STEPS[W.step];
  const stepper = h('div', { class: 'stepper', role: 'img', 'aria-label': t('setup.step', { n: W.step + 1, total: STEPS.length }) },
    h('span', { class: 'label', 'aria-hidden': 'true' }, t('setup.step', { n: W.step + 1, total: STEPS.length })),
    STEPS.map((s, i) => h('span', { class: i < W.step ? 'done' : i === W.step ? 'current' : '' })));
  // the whole way through, named: where you are, what is done, what is left
  const steps = h('ol', { class: 'setup-steps', 'aria-hidden': 'true' }, STEPS.map((s, i) =>
    h('li', { class: i < W.step ? 'done' : i === W.step ? 'current' : '' }, i < W.step ? icon('check') : h('span', { class: 'dot' }), t('setup.nav.' + s))));
  const top = h('div', { class: 'setup-top' },
    h('div', { class: 'brand' }, h('span', { class: 'brand-mark' }, brandMark()), h('span', {}, 'Voicestream')),
    stepper, steps,
    W.forced && btn(t('setup.close'), { cls: 'ghost small', onclick: closeSetup }, 'x'));
  let body;
  if (!S.settings || !S.models || !W.choice) {
    body = [h('h1', { id: 'setup-h' }, t('setup.title')), h('p', { class: 'lead' }, W.error || t('common.loading'))];
  } else {
    body = SETUP_STEPS[step].view(W);
  }
  W.foot = h('div', { class: 'setup-foot', id: 'setup-foot' });
  W.errorBox = h('div', { id: 'setup-error-box' });
  // a choice redraws the step it is on: its control keeps the focus (an arrow key goes on through a
  // radio group); a new step's title takes it instead, below
  const focused = document.activeElement && root.contains(document.activeElement) ? document.activeElement.id : null;
  fill(root, top,
    h('div', { class: 'setup-main' }, h('section', { class: 'card setup-card', 'aria-labelledby': 'setup-h', id: 'setup-' + step }, body, W.errorBox)),
    W.foot);
  renderSetupFoot();
  if (W.shown === W.step && focused && $(focused)) $(focused).focus({ preventScroll: true });
  if (W.shown !== W.step) {
    W.shown = W.step;
    const heading = $('setup-h');
    if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus({ preventScroll: true }); }
    root.scrollTop = 0;
  }
}

function renderSetupFoot() {
  const W = S.setup;
  if (!W || !W.foot) return;
  const def = SETUP_STEPS[STEPS[W.step]];
  const ready = S.settings && S.models && W.choice;
  const dlName = Object.keys(S.downloads).find((n) => S.models && (S.models.models.find((m) => m.name === n) || {}).kind === 'whisper');
  const m = dlName && S.models.models.find((x) => x.name === dlName);
  const dl = dlName ? h('div', { class: 'dl', title: modelLabel(m) }, icon('download'), h('span', { class: 'dl-label' }, t('setup.downloading')), progressBar(dlName, S.downloads[dlName]))
    : h('div', { class: 'dl' });
  const back = W.step > 0 && W.step < STEPS.length && btn(t('setup.back'), { id: 'setup-back', disabled: W.busy, onclick: () => setStep(W.step - 1) }, 'back');
  const skip = ready && def.skip && btn(def.skip.label(), { id: 'setup-skip', cls: 'ghost', disabled: W.busy, onclick: () => runStep(def.skip.run) });
  const nextLabel = ready ? def.nextLabel() : t('setup.next');
  const next = h('button', { type: 'button', class: 'btn primary big', id: 'setup-next', disabled: !ready || W.busy, onclick: () => runStep(def.next) },
    W.busy ? spinner() : null, nextLabel);
  if (W.errorBox) {
    fill(W.errorBox, W.error && ready ? h('p', { class: 'field-error setup-error', role: 'alert', id: 'setup-error' }, W.error) : null,
      W.error && ready && W.fileProblem ? btn(t('fix.show_config'), { cls: 'small', id: 'setup-show-config', onclick: () => reveal('config') }, 'folder') : null);
  }
  const extra = ready && def.secondary && (!def.secondary.when || def.secondary.when(W))
    ? btn(def.secondary.label(), { id: 'setup-secondary', disabled: W.busy, onclick: () => runStep(def.secondary.run) }) : null;
  fill(W.foot, back || h('span'), dl, skip, extra, next);
}

/** Run a step's action; ``true`` from it moves to the next step. */
async function runStep(fn) {
  const W = S.setup;
  if (!W || W.busy) return;
  W.busy = true;
  W.error = null;
  renderSetupFoot();
  W.fileProblem = false;
  W.sent = [];
  let ok = false;
  try {
    ok = await fn(W);
  } catch (err) {
    if (err.status === 400 && err.body) {
      const field = err.body.field || '';
      const own = W.sent.some((k) => field && (field === k || field.startsWith(k + '.') || k.startsWith(field + '.')));
      // refused for something this step didn't send (a tool turned on by hand in the settings file…):
      // say what and where, not as if the name you typed were wrong
      W.fileProblem = !own;
      W.error = own ? friendlyError(field, err.body.error)
        : t('setup.file_problem', { what: fieldLabel(field), detail: friendlyError(field, err.body.error) });
    } else {
      W.error = humanize(err.message);
    }
  }
  if (S.setup !== W) return;  // finished
  W.busy = false;
  if (ok) setStep(W.step + 1);
  else renderSetup();
}

async function saveSetting(changes) {
  if (S.setup) S.setup.sent = Object.keys(changes);
  await api('PATCH', '/api/settings', { changes });
  await loadSettings();
}

/** A setting's place in words: the tool's name for ``sinks.<tool>…``. */
function fieldLabel(field) {
  const tool = (/^sinks\.([\w-]+)/.exec(field) || [])[1];
  if (tool) return has('tool.' + tool + '.name') ? t('tool.' + tool + '.name') : tool;
  return field || t('setup.file_problem_file');
}

function setupHead(title, lead) {
  return [h('h1', { id: 'setup-h' }, title), lead && h('p', { class: 'lead' }, lead)];
}

const SETUP_STEPS = {
  welcome: {
    view: () => [
      ...setupHead(t('setup.welcome.title'), t('setup.welcome.lead')),
      h('div', { class: 'promises' }, [['shield', 'p1'], ['mic', 'p2'], ['sparkle', 'p3'], ['clock', 'p4']].map(([ic, k]) =>
        h('div', { class: 'promise' }, icon(ic),
          h('div', {}, h('strong', {}, t('setup.welcome.' + k)), h('span', {}, t('setup.welcome.' + k + '_text')))))),
    ],
    nextLabel: () => t('setup.welcome.start'),
    next: async () => true,
  },

  you: {
    view: (W) => {
      const name = h('input', { type: 'text', id: 'setup-name', value: W.choice.name, autocomplete: 'name', placeholder: t('set.name_ph') });
      name.addEventListener('input', () => { W.choice.name = name.value; if (W.error) { W.error = null; renderSetupFoot(); } });
      name.addEventListener('keydown', (e) => { if (e.key === 'Enter') runStep(SETUP_STEPS.you.next); });
      return [
        ...setupHead(t('setup.you.title'), t('setup.you.lead')),
        h('div', { class: 'field' }, h('label', { for: 'setup-name' }, t('set.name')), h('div', { class: 'control' }, name),
          h('p', { class: 'help' }, t('set.name_help'))),
        h('div', { class: 'field' }, h('span', { class: 'label' }, t('setup.you.languages')),
          languageCards(t('setup.you.languages'), W.choice.lang, (v) => { W.choice.lang = v; renderSetup(); })),
      ];
    },
    nextLabel: () => t('setup.next'),
    next: async (W) => {
      const name = W.choice.name.trim();
      if (!name) { W.error = t('setup.you.need_name'); return false; }
      await saveSetting({ user_name: name, ...(LANG_CHOICES[W.choice.lang] || {}) });
      S.userName = name;
      return true;
    },
  },

  stt: {
    view: (W) => {
      const pick = (v) => { W.choice.stt = v; renderSetup(); if (v === 'whisper-cpp') startRecommendedDownload(); };
      const tc = S.settings.values.transcription;
      const options = [
        { value: 'whisper-cpp', title: t('stt.local') + ' · ' + t('stt.recommended'), desc: t('setup.stt.local_desc', { size: fmtBytes((S.models.models.find((m) => m.recommended) || {}).size) }) },
        { value: 'openai', title: t('stt.openai'), desc: t('stt.cloud_desc', { price: sttPrice(tc.openai.model) || '—' }) },
        { value: 'deepgram', title: t('stt.deepgram'), desc: t('stt.cloud_desc', { price: sttPrice(tc.deepgram.model) || '—' }) },
      ];
      let detail;
      if (W.choice.stt === 'whisper-cpp') {
        detail = h('div', { class: 'sub-card' }, modelList({ only: [recommendedModel()], noRemove: true }),
          h('p', { class: 'help small muted' }, t('setup.stt.background')));
      } else {
        const key = W.choice.stt === 'openai' ? 'OPENAI_API_KEY' : 'DEEPGRAM_API_KEY';
        detail = h('div', { class: 'sub-card' }, keyField(key, { label: t(W.choice.stt === 'openai' ? 'key.openai' : 'key.deepgram'), onChange: () => { W.error = null; renderSetupFoot(); } }),
          h('p', { class: 'small' }, h('a', { href: SECRET_LINKS[key], target: '_blank', rel: 'noopener noreferrer' },
            t(W.choice.stt === 'openai' ? 'key.where_openai' : 'key.where_deepgram'), ' ', icon('external'))));
      }
      return [...setupHead(t('setup.stt.title'), t('setup.stt.lead')), choiceCards(t('setup.stt.title'), options, W.choice.stt, pick, { list: true }), detail];
    },
    nextLabel: () => t('setup.next'),
    next: async (W) => {
      const backend = W.choice.stt;
      if (backend !== 'whisper-cpp') {
        const key = backend === 'openai' ? 'OPENAI_API_KEY' : 'DEEPGRAM_API_KEY';
        if (!S.settings.secrets[key]) { W.error = t('setup.stt.need_key'); return false; }
        await saveSetting({ 'transcription.backend': backend });
      } else {
        await saveSetting({ 'transcription.backend': 'whisper-cpp', 'transcription.whisper_cpp.model': recommendedModel() });
      }
      return true;
    },
  },

  claude: {
    view: () => [
      ...setupHead(t('setup.claude.title'), t('setup.claude.lead')),
      keyField('ANTHROPIC_API_KEY', { label: t('key.claude'), onChange: () => { S.setup.error = null; renderSetupFoot(); } }),
      h('p', {}, h('a', { href: SECRET_LINKS.ANTHROPIC_API_KEY, target: '_blank', rel: 'noopener noreferrer', id: 'where-claude' }, t('key.where_claude'), ' ', icon('external'))),
      h('p', { class: 'help muted' }, t('setup.claude.cost')),
      h('p', { class: 'help muted' }, t('setup.claude.privacy')),
    ],
    nextLabel: () => t('setup.next'),
    next: async (W) => {
      if (!S.settings.secrets.ANTHROPIC_API_KEY) { W.error = t('setup.claude.need_key'); return false; }
      await saveSetting({ 'brain.provider': 'anthropic' });
      return true;
    },
    skip: {
      label: () => t('setup.claude.skip'),
      run: async () => { await saveSetting({ 'brain.provider': 'heuristic' }); return true; },
    },
  },

  perms: {
    view: () => [
      ...setupHead(t('setup.perms.title'), t('setup.perms.lead')),
      permissionRow('microphone'),
      permissionRow('system-audio'),
      h('p', { class: 'help muted' }, S.app.platform === 'darwin' ? t('setup.perms.help_mac') : t('setup.perms.help_other')),
    ],
    nextLabel: () => t('setup.next'),
    next: async () => true,
  },

  tools: {
    view: (W) => {
      const mac = S.app.platform === 'darwin';
      const list = h('input', { type: 'text', id: 'setup-list', value: W.choice.list, autocomplete: 'off' });
      list.addEventListener('input', () => { W.choice.list = list.value; });
      const rc = W.remindersCheck;
      const checkLine = W.choice.reminders && (W.busy && W.checkingReminders
        ? h('p', { class: 'small muted', role: 'status', id: 'setup-reminders-check' }, spinner(), ' ', t('tools.reminders.asking'))
        : rc && h('p', { class: 'small ' + (rc.status === 'ok' ? 'ok-text' : 'error-text'), role: 'status', id: 'setup-reminders-check' },
          ...toolCheckLine(rc), rc.status === 'ok' ? null : h('span', {}, ' ', btn(t('setup.tools.check_again'), {
            cls: 'small', id: 'setup-reminders-again', onclick: () => { W.remindersCheck = null; runStep(SETUP_STEPS.tools.next); } }))));
      return [
        ...setupHead(t('setup.tools.title'), t('setup.tools.lead')),
        h('div', { class: 'sub-card' },
          switchRow('setup-reminders', t('tool.reminders.name'), mac ? t('setup.tools.reminders_help') : t('setup.tools.reminders_not_mac'), W.choice.reminders,
            (on) => { W.choice.reminders = on; W.remindersCheck = null; renderSetup(); }),
          W.choice.reminders && h('div', { class: 'field' }, h('label', { for: 'setup-list' }, t('tools.reminders.list')), h('div', { class: 'control' }, list)),
          checkLine),
        h('div', { class: 'sub-card' }, h('div', { class: 'promise' }, icon('folder'),
          h('div', {}, h('strong', {}, t('tool.markdown.name')), h('span', {}, t('setup.tools.files'))))),
        h('p', { class: 'help muted' }, t('setup.tools.later')),
      ];
    },
    nextLabel: () => t('setup.next'),
    next: async (W) => {
      const changes = { 'sinks.reminders.enabled': W.choice.reminders };
      if (W.choice.reminders) changes['sinks.reminders.list_name'] = W.choice.list.trim();
      await saveSetting(changes);
      if (!W.choice.reminders) return true;
      // a problem already shown: carry on (it can be fixed later, Settings → Your tools → Test)
      if (W.remindersCheck && W.remindersCheck.status !== 'ok') return true;
      // check Reminders now: macOS asks whether Voicestream may control it while you are here,
      // not at the first meeting's wrap-up, when nobody may be there to answer
      W.checkingReminders = true;
      renderSetup();
      try {
        W.remindersCheck = await checkTool('reminders') || { status: 'fail', detail: t('tools.test_off') };
      } finally {
        W.checkingReminders = false;
      }
      return W.remindersCheck.status === 'ok';
    },
  },

  start: {
    // when Voicestream records, and the ways of starting a meeting from any app: the defaults are on,
    // so Continue is enough; what only the Mac app does is said instead of offered elsewhere
    view: (W) => {
      const caps = S.app.capabilities || {};
      const c = W.choice;
      const options = [
        { value: 'manual', title: t('start.manual'), desc: t('setup.start.manual_desc') },
        { value: 'automatic', title: t('start.automatic'), desc: t('start.automatic_desc') },
      ];
      const parts = [...setupHead(t('setup.start.title'), t('setup.start.lead')),
        choiceCards(t('setup.start.title'), options, c.mode, (v) => { c.mode = v; renderSetup(); }, { list: true })];
      const automatic = c.mode === 'automatic';
      if (automatic) {
        parts.push(h('div', { class: 'sub-card' },
          switchRow('setup-launch', t('set.listen_on_launch'), t('set.listen_on_launch_help'), c.listenOnLaunch, (on) => { c.listenOnLaunch = on; })));
      }
      // what puts something on screen, writes an app into ~/Applications or watches the microphone
      // (all on unless turned off here, either way of starting meetings: a meeting can be started by
      // hand in automatic mode too), then by hand the shortcut, which does none of that; the last step
      // lists the ones on, as this one can be taller than the window
      if (caps.floating_button || caps.spotlight || caps.call_detection) {
        parts.push(h('div', { class: 'sub-card' },
          caps.floating_button && switchRow('setup-floating', t('start.floating'), t('start.floating_help'), c.floating, (on) => { c.floating = on; }),
          caps.spotlight && switchRow('setup-spotlight', t('start.spotlight'), t('start.spotlight_help'), c.spotlight, (on) => { c.spotlight = on; }),
          caps.call_detection && switchRow('setup-suggestions', t('start.suggest'), t('start.suggest_help'), c.suggestions, (on) => { c.suggestions = on; })));
      }
      if (automatic) return parts;
      if (caps.hotkey) {
        W.shortcut = W.shortcut || shortcutSetting({ prefix: 'setup-shortcut', variant: 'setup' });
        W.shortcut.draw();
        parts.push(h('div', { class: 'sub-card setup-shortcut' }, W.shortcut));
      }
      if (!caps.hotkey && !(caps.floating_button || caps.spotlight || caps.call_detection)) {  // the browser: none of these exist here
        parts.push(h('p', { class: 'help muted', id: 'setup-start-mac-only' }, t('setup.start.mac_only')));
      }
      return parts;
    },
    nextLabel: () => t('setup.next'),
    next: async (W) => {
      const c = W.choice, p = S.app.prefs;
      const want = { meeting_start: c.mode, floating_button: c.floating, spotlight_launcher: c.spotlight,
        call_suggestions: c.suggestions, listen_on_launch: c.listenOnLaunch };
      const changes = Object.fromEntries(Object.entries(want).filter(([k, v]) => p[k] !== v));
      if (Object.keys(changes).length) {
        const saved = await api('PATCH', '/api/prefs', changes);
        S.app = { ...S.app, prefs: { ...S.app.prefs, ...saved }, mode: saved.meeting_start || S.app.mode };
      }
      return true;
    },
  },

  done: {
    view: (W) => {
      const v = S.settings.values;
      const local = v.transcription.backend === 'whisper-cpp';
      const model = local && v.transcription.whisper_cpp.model;
      const downloading = local && S.downloads[model];
      const m = local && S.models.models.find((x) => x.name === model);
      const claude = v.brain.provider === 'anthropic' && S.settings.secrets.ANTHROPIC_API_KEY;
      const rem = S.settings.sinks.reminders && S.settings.sinks.reminders.enabled;
      const row = (ok, text) => h('div', { class: ok ? '' : 'pending' }, icon(ok ? 'checkCircle' : 'clock'), h('span', {}, text));
      const caps = S.app.capabilities || {};
      const hk = S.app.prefs.hotkey;
      const how = W.choice.mode === 'automatic' ? t('setup.done.automatic')
        : hk && caps.hotkey ? t('setup.done.manual_keys', { keys: shortcutText(hk) }) : t('setup.done.manual');
      // what the start step turned on, said here in full, either way of starting meetings (they work in
      // both): a floating button, an app in your Applications folder, the microphone watched for calls
      const c = W.choice;
      const ways = [caps.floating_button && c.floating && t('setup.done.floating'),
        caps.spotlight && c.spotlight && t('setup.done.spotlight'),
        caps.call_detection && c.suggestions && t('setup.done.suggest')].filter(Boolean);
      return [
        ...setupHead(t('setup.done.title', { name: v.user_name }), t('setup.done.lead')),
        h('div', { class: 'summary-list' },
          row(true, t('setup.done.lang', { lang: t('lang.' + (langChoice(savedVal) === 'other' ? 'any' : langChoice(savedVal))) })),
          local ? row(m && m.installed, m && m.installed ? t('setup.done.stt_local') : downloading ? t('setup.done.stt_downloading') : t('setup.done.stt_missing'))
            : row(true, t('setup.done.stt_cloud', { name: v.transcription.backend === 'deepgram' ? 'Deepgram' : 'OpenAI' })),
          row(true, claude ? t('setup.done.claude') : t('setup.done.offline')),
          rem ? row(!W.remindersCheck || W.remindersCheck.status === 'ok',
            t('setup.done.reminders', { list: savedVal('sinks.reminders.list_name') || '' })) : null,
          row(true, t('setup.done.files')),
          row(true, how),
          ways.map((text) => row(true, text))),
        caps.login_item ? h('div', { class: 'sub-card' },
          switchRow('setup-login', t('set.start_at_login'), t('set.login_help'), W.choice.startAtLogin, (on) => { W.choice.startAtLogin = on; })) : null,
      ];
    },
    // by hand, nothing is started now: Done opens Live, its Start meeting ready for the next call
    nextLabel: () => {
      if (S.setup && S.setup.choice.mode !== 'automatic') return t('setup.done.finish');
      const v = S.settings.values;
      const model = v.transcription.backend === 'whisper-cpp' && v.transcription.whisper_cpp.model;
      return model && S.downloads[model] ? t('setup.done.start_after') : t('setup.done.start');
    },
    next: async (W) => finishSetup(W, W.choice.mode === 'automatic'),
    secondary: { label: () => t('setup.done.later'), run: async (W) => finishSetup(W, false), when: (W) => W.choice.mode === 'automatic' },
  },
};

async function finishSetup(W, start) {
  const prefs = {};
  if (W.choice.startAtLogin !== S.app.prefs.start_at_login && S.app.capabilities.login_item) prefs.start_at_login = W.choice.startAtLogin;
  if (W.choice.listenOnLaunch !== S.app.prefs.listen_on_launch) prefs.listen_on_launch = W.choice.listenOnLaunch;
  if (Object.keys(prefs).length) await api('PATCH', '/api/prefs', prefs);
  const v = S.settings.values;
  const model = v.transcription.backend === 'whisper-cpp' && v.transcription.whisper_cpp.model;
  S.setupCompleted = true;  // an app.state sent before this one must not reopen the wizard
  await api('POST', '/api/setup/complete');
  S.app.setup_done = true;
  if (S.setup === W) closeSetup();
  location.hash = '#/live';
  if (start) {
    if (model && S.downloads[model]) {
      S.startAfterDownload = model;
      toast(t('setup.done.will_start'), 'info');
    } else {
      setApp(await api('POST', '/api/listen/start'));
    }
  }
  return false;
}

// ================================================================= keyboard shortcut
// The shortcut that starts and stops listening from any app (Settings → General). The Mac app
// registers it with macOS (voicestream/app/hotkey.py, whose rules these are: keep the two in step)
// and says whether macOS took it. The key is the physical one (KeyboardEvent.code), so the shortcut
// stays on the same key whatever the keyboard layout; what is printed on it (KeyboardEvent.key) is
// only shown — and looked at for the commands menus give the character a key types (⌘Q, ⌘W…).

const SHORTCUT_MODS = ['ctrl', 'alt', 'shift', 'cmd'];  // in macOS's order: ⌃⌥⇧⌘
const SHORTCUT_GLYPHS = { ctrl: '⌃', alt: '⌥', shift: '⇧', cmd: '⌘' };
const SHORTCUT_EVENT_MODS = { ctrl: 'ctrlKey', alt: 'altKey', shift: 'shiftKey', cmd: 'metaKey' };
const SHORTCUT_DEFAULT = { code: 'KeyR', mods: ['alt', 'cmd'], key: 'R' };
const SHORTCUT_PUNCT = {
  Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']', Backslash: '\\', Semicolon: ';', Quote: "'",
  Comma: ',', Period: '.', Slash: '/', Backquote: '`', IntlBackslash: '§',
};
const SHORTCUT_SPECIAL = {
  Space: null, Enter: '↩', Tab: '⇥', Backspace: '⌫', Escape: '⎋', ArrowLeft: '←', ArrowRight: '→', ArrowDown: '↓', ArrowUp: '↑',
};  // Space is a word (shortcut.key.space)
const SHORTCUT_RESERVED = Object.fromEntries([
  // every Mac app's own commands: Quit, Close, Hide, Minimize, Settings (and their ⌥ variants)
  ...['cmd+KeyQ', 'cmd+KeyW', 'cmd+KeyH', 'cmd+KeyM', 'cmd+Comma', 'alt+cmd+KeyH', 'alt+cmd+KeyW', 'alt+cmd+KeyM'].map((c) => [c, 'app']),
  // macOS's own: switching apps and windows, Spotlight, input sources, Force Quit, locking, screenshots…
  ...['cmd+Tab', 'shift+cmd+Tab', 'cmd+Backquote', 'shift+cmd+Backquote', 'cmd+IntlBackslash', 'shift+cmd+IntlBackslash',
    'cmd+Space', 'alt+cmd+Space', 'ctrl+Space', 'ctrl+alt+Space', 'ctrl+cmd+Space', 'alt+cmd+Escape',
    'ctrl+cmd+KeyQ', 'shift+cmd+KeyQ', 'alt+shift+cmd+KeyQ', 'shift+cmd+Digit3', 'shift+cmd+Digit4', 'shift+cmd+Digit5',
    'shift+cmd+Digit6', 'ctrl+shift+cmd+Digit3', 'ctrl+shift+cmd+Digit4', 'ctrl+cmd+KeyF', 'alt+cmd+KeyD',
    'ctrl+ArrowLeft', 'ctrl+ArrowRight', 'ctrl+ArrowUp', 'ctrl+ArrowDown', 'cmd+F5', 'alt+cmd+F5',
    // keyboard navigation: keyboard access, focus on the menu bar, the Dock, a window, the toolbar…
    'ctrl+F1', 'ctrl+F2', 'ctrl+F3', 'ctrl+F4', 'ctrl+F5', 'ctrl+F6', 'ctrl+F7', 'ctrl+F8',
  ].map((c) => [c, 'mac']),
]);

function shortcutUsable(code) {
  return /^Key[A-Z]$|^Digit[0-9]$|^F([1-9]|1[0-9])$/.test(code) || Object.hasOwn(SHORTCUT_PUNCT, code) || Object.hasOwn(SHORTCUT_SPECIAL, code);
}

function shortcutMods(mods) { return SHORTCUT_MODS.filter((m) => (mods || []).includes(m)); }

/** What is printed on the key: its label here when that is a plain character, else a US keyboard's. */
function shortcutLabel(code, key) {
  const k = String(key || '').trim();
  if ((/^Key/.test(code) || Object.hasOwn(SHORTCUT_PUNCT, code)) && [...k].length === 1 && /^[^\p{C}\p{Z}]$/u.test(k)) {
    const upper = k.toUpperCase();
    return [...upper].length === 1 ? upper : k;  // the German ß is ß on the key, not SS
  }
  if (/^Key|^Digit/.test(code)) return code.replace(/^Key|^Digit/, '');
  if (Object.hasOwn(SHORTCUT_PUNCT, code)) return SHORTCUT_PUNCT[code];
  if (code === 'Space') return t('shortcut.key.space');
  return SHORTCUT_SPECIAL[code] || code;
}

/** The US key for what this one types (the AZERTY key printed Q is KeyQ here), for the menu commands. */
function shortcutTyped(code, key) {
  if (!/^Key/.test(code) && !Object.hasOwn(SHORTCUT_PUNCT, code)) return code;
  const char = shortcutLabel(code, key);
  if (/^[A-Z]$/.test(char)) return 'Key' + char;
  return Object.keys(SHORTCUT_PUNCT).find((c) => SHORTCUT_PUNCT[c] === char) || code;
}

/** Why a combination can't be the shortcut (shortcut.why.<reason>), or null. */
function shortcutProblem(code, mods, key) {
  if (!shortcutUsable(code)) return 'unknown_key';
  const held = shortcutMods(mods);
  const strong = held.filter((m) => m !== 'shift');
  if (!strong.length) return 'needs_modifier';
  const reserved = SHORTCUT_RESERVED[[...held, shortcutTyped(code, key)].join('+')];
  if (reserved) return 'reserved_' + reserved;
  if (strong.length === 1 && !/^F\d+$/.test(code)) return 'one_modifier';
  if (strong.length === 1 && strong[0] === 'alt') return 'option_only';  // macOS 15 can refuse it to every app
  return null;
}

function shortcutText(hk) {
  return shortcutMods(hk.mods).map((m) => SHORTCUT_GLYPHS[m]).join('') + shortcutLabel(hk.code, hk.key);
}

/** A keydown as a shortcut. With ⌥ held a Mac gives the character ⌥ types (⌥R is ®, ⌥A is æ on a French
 *  keyboard) — unless ⌃ is held too: WebKit then gives what the key types without them — and with ⇧ the
 *  shifted one. The label then comes from what the Mac app read from macOS: what each key types with
 *  nothing held on the keyboard layout in use (S.app.keyboard_layout, as the server checks it too);
 *  without it (the browser), from the physical key. */
function shortcutFromEvent(e) {
  const mods = SHORTCUT_MODS.filter((m) => e[SHORTCUT_EVENT_MODS[m]]);
  const plain = (!e.altKey || e.ctrlKey) && !(e.shiftKey && Object.hasOwn(SHORTCUT_PUNCT, e.code));
  if (plain && [...e.key].length === 1) return { code: e.code, mods, key: e.key };
  const layout = (S.app && S.app.keyboard_layout) || {};
  return { code: e.code, mods, key: Object.hasOwn(layout, e.code) ? String(layout[e.code]) : '' };
}

/** The Live screen's line for when it isn't listening (always-on listening): the shortcut, once macOS took it. */
function shortcutHint() {
  const a = S.app;
  const hk = a && a.prefs.hotkey;
  if (!hk || !a.capabilities.hotkey || !a.hotkey || a.hotkey.status !== 'ok') return null;
  return h('span', { id: 'shortcut-hint' }, ' ' + t('shortcut.hint', { keys: shortcutText(hk) }));
}

/** A shortcut as the keys to press: ⌥ ⌘ R. */
function shortcutKeys(hk, attrs = {}) {
  const caps = [...shortcutMods(hk.mods).map((m) => SHORTCUT_GLYPHS[m]), shortcutLabel(hk.code, hk.key)];
  return h('span', { class: 'keys', translate: 'no', ...attrs }, caps.map((c) => h('kbd', {}, c)));
}

/** The keyboard shortcut, with "Choose a shortcut" (records the next combination pressed), and in
 *  Settings Turn off, Reset and the sound it plays. Saved at once, as macOS's own keyboard settings are:
 *  whether macOS took it is what you want to know next. Elsewhere than the Mac app, it says it is a Mac
 *  feature. Three places show it: Settings → General (``settings``), Live between meetings (``live``:
 *  the keys in a sentence) and the setup (``setup``: a large button); ``prefix`` makes their ids.
 *  The element's ``draw()`` shows the shortcut as it is now and keeps a recording under way. */
function shortcutSetting({ prefix = 'shortcut', variant = 'settings' } = {}) {
  const id = (part) => prefix + '-' + part;
  const box = h('div', { id: id('setting'), class: 'shortcut-setting sc-' + variant, dataset: variant === 'settings' ? { path: 'pref:hotkey' } : null });
  let recording = false, error = '', timer = null;
  const mac = () => Boolean(S.app.capabilities && S.app.capabilities.hotkey);

  const tellHost = (active) => api('POST', '/api/hotkey/recording', { active }).catch(() => { /* back on by itself */ });
  const stop = (tell) => {
    if (!recording) return;
    recording = false;
    clearTimeout(timer);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('blur', onBlur);
    if (tell) tellHost(false);
  };
  const record = () => {
    recording = true;
    error = '';
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('blur', onBlur);  // gone to another app: that press is not for us
    timer = setTimeout(() => { stop(true); draw(); }, 30000);  // the app turns it back on by then too
    tellHost(true);  // the current shortcut is off meanwhile, or pressing it would not reach this page
    draw();
    $(id('keys')).focus({ preventScroll: true });  // not the button: Space or Enter would press it
  };
  const onBlur = () => { stop(true); draw(); };
  const backToChange = () => { if (box.isConnected && $(id('change'))) $(id('change')).focus({ preventScroll: true }); };
  function onKey(e) {
    if (!box.isConnected) { stop(true); return; }
    e.preventDefault();
    e.stopPropagation();
    const held = SHORTCUT_MODS.filter((m) => e[SHORTCUT_EVENT_MODS[m]]);
    if (/^(Shift|Control|Alt|Meta|OS)(Left|Right)$|^CapsLock$|^Fn/.test(e.code) || ['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'Fn'].includes(e.key)) {
      draw(held);  // only modifiers so far: show them
      return;
    }
    if (e.code === 'Escape' && !held.length) { stop(true); error = ''; draw(); backToChange(); return; }
    const hk = shortcutFromEvent(e);
    const why = shortcutProblem(hk.code, hk.mods, hk.key);
    if (why) { error = t('shortcut.why.' + why, { keys: shortcutText(hk) }); draw(); return; }  // try another
    stop(false);  // saving it ends the recording
    save(hk, null);
  }
  const save = async (hk, button) => {
    error = '';
    const res = await act(button, async () => {
      await api('PATCH', '/api/prefs', { hotkey: hk });
      setApp(await api('GET', '/api/app'));
      return true;
    });
    if (!res) { tellHost(false); draw(); return; }
    draw();
    // the focus was on the keys shown (recorded from the keyboard) or on a button that is gone now
    if (!document.activeElement || document.activeElement === document.body) backToChange();
    // the app registers it with macOS and says what macOS answered: wait for it (a moment)
    for (let i = 0; i < 20 && mac() && S.app.hotkey && S.app.hotkey.status === 'pending'; i++) {
      await new Promise((resolve) => setTimeout(resolve, 150));
      try { setApp(await api('GET', '/api/app')); } catch (_) { break; }
    }
    if (box.isConnected) draw();
  };
  // what the shortcut does follows how meetings start: a meeting, or listening
  const said = (key) => t(isManual() ? key + '_meeting' : key);
  const statusText = (hk) => {
    const st = S.app.hotkey || {};
    if (!hk) return said('shortcut.status.off');
    if (st.status === 'ok') return said('shortcut.status.ok');
    if (st.status === 'taken') return t('shortcut.status.taken');
    if (st.status === 'failed') return t('shortcut.status.failed', { code: st.error });
    return '';  // pending: the answer is on its way
  };
  const draw = (held) => keepFocus(box, () => {
    if (!mac()) {  // the browser, or Linux and Windows: no global shortcut, said so where it would be
      fill(box, variant === 'live' ? null : variant === 'setup'
        ? h('p', { class: 'help', id: id('status') }, said('shortcut.mac_only'))
        : h('div', { class: 'field' }, h('span', { class: 'label', id: id('label') }, t('shortcut.label')),
          h('p', { class: 'help', id: id('status'), role: 'status' }, said('shortcut.mac_only'))));
      return;
    }
    const hk = S.app.prefs.hotkey;
    const st = (S.app.hotkey || {}).status;
    const isDefault = hk && hk.code === SHORTCUT_DEFAULT.code && shortcutMods(hk.mods).join() === SHORTCUT_DEFAULT.mods.join();
    let keys;
    if (recording) {
      keys = h('span', { class: 'pill info', id: id('keys'), tabindex: '-1' },
        held && held.length ? held.map((m) => SHORTCUT_GLYPHS[m]).join('') + '…' : t('shortcut.press'));
    } else {
      keys = hk ? shortcutKeys(hk, { id: id('keys') }) : h('span', { class: 'pill', id: id('keys') }, t('shortcut.off'));
    }
    const choose = btn(recording ? t('common.cancel') : t('shortcut.choose'), {
      cls: variant === 'setup' ? 'big' : 'small', id: id('change'),
      onclick: () => { if (recording) { stop(true); draw(); } else record(); } }, recording || variant === 'settings' ? null : 'keyboard');
    const off = !recording && hk && btn(t('shortcut.turn_off'), { cls: 'small ghost', id: id('off'), onclick: (e) => save(null, e.currentTarget) });
    const reset = !recording && !isDefault && btn(t('shortcut.reset', { keys: shortcutText(SHORTCUT_DEFAULT) }), {
      cls: 'small ghost', id: id('reset'), onclick: (e) => save({ ...SHORTCUT_DEFAULT }, e.currentTarget) });
    const errorLine = h('p', { class: 'field-error', id: id('error'), role: error ? 'alert' : null }, error);
    if (variant === 'live') {
      // a sentence: "Or press ⌥ ⌘ R in any app", and the button beside it
      let line;
      if (recording) line = [keys];
      else if (!hk) line = [h('span', { id: id('keys') }, t('live.shortcut.none'))];
      else if (st === 'taken' || st === 'failed') line = [keys, ' ', h('span', { class: 'warn-text' }, t(st === 'taken' ? 'live.shortcut.taken' : 'live.shortcut.failed'))];
      else line = [t('live.shortcut.or_press'), ' ', keys, ' ', t('live.shortcut.any_app')];
      fill(box,
        h('div', { class: 'shortcut-line', role: 'group', 'aria-labelledby': id('text') },
          h('span', { class: 'shortcut-text', id: id('text') }, ...line), choose),
        recording && h('p', { class: 'help', id: id('status'), role: 'status' }, t('shortcut.press_help')),
        errorLine);
      return;
    }
    const help = recording ? t('shortcut.press_help') : statusText(hk);
    const controls = variant === 'setup' ? [keys, choose, reset] : [keys, choose, off, reset];
    fill(box,
      h('div', { class: 'field' + (error ? ' invalid' : '') },
        h('span', { class: 'label', id: id('label') }, t('shortcut.label')),
        h('div', { class: 'control', role: 'group', 'aria-labelledby': id('label'), 'aria-describedby': id('status') + ' ' + id('error') },
          ...controls),
        h('p', { class: 'help', id: id('status'), role: 'status' }, help),
        errorLine),
      variant === 'settings' && prefSwitch('hotkey_sound', said('shortcut.sound'), said('shortcut.sound_help')));
  });
  box.draw = draw;
  draw();
  return box;
}

// ========================================================================== strings
// Every word the app shows, in English and French. Plain words for people who never open a
// terminal: "speech-to-text on this Mac", never "whisper-cpp backend".

const STRINGS = {
  en: {
    'boot.opening': 'Opening Voicestream…',
    'boot.failed': 'Voicestream could not start: {detail}',

    'common.close': 'Close', 'common.ok': 'OK', 'common.cancel': 'Cancel', 'common.save': 'Save', 'common.open': 'Open',
    'common.loading': 'Loading…', 'common.skip': 'Skip to content', 'common.hide': 'Hide',

    'err.unreachable': 'Can’t reach Voicestream: check that it is still open, then try again.',
    'err.minutes': 'Enter a whole number of minutes, from 0 to 600.',
    'err.end_before_ask': 'Choose more minutes than for “Still in a meeting?” ({n}), or the question is never asked. To never ask, set that to 0.',
    'err.copy': "Couldn't copy to the clipboard.",
    'err.required': "This can't be empty.",
    'err.number': 'Enter a number.',
    'err.format': "This doesn't look right — check the format shown below the field.",
    'err.shortcuts': 'Enter the name of at least one shortcut.',
    'err.command': 'Enter at least one command.',
    'err.range': 'This value is out of range ({detail}).',
    'file.cannot_open': "Can't open {name}",

    'conn.connecting': 'Connecting…', 'conn.live': 'Connected', 'conn.retry': 'Reconnecting…', 'conn.dead': 'Not connected',
    'conn.auth': 'This page needs the link Voicestream opened: it carries a key that changes every time the app starts. Open Voicestream again.',
    'conn.lost': 'Lost the connection to Voicestream. Trying again…',

    'nav.label': 'Main', 'nav.live': 'Live', 'nav.meetings': 'Meetings', 'nav.costs': 'Costs', 'nav.settings': 'Settings',
    'nav.live_now': 'Now',
    'nav.menu': 'Menu',

    'state.stopped': 'Not listening', 'state.starting': 'Starting…', 'state.listening': 'Listening',
    'state.paused': 'Paused — nothing is being recorded', 'state.paused_short': 'Paused', 'state.stopping': 'Stopping…',
    'state.error': 'Needs attention', 'state.meeting_for': 'Meeting in progress · {time}', 'state.in_meeting': 'A meeting is in progress',
    'state.m.stopped': 'Not recording', 'state.m.listening': 'Recording', 'state.m.stopping': 'Wrapping up…',

    'action.start': 'Start listening', 'action.pause': 'Pause', 'action.resume': 'Resume', 'action.stop': 'Stop',
    'action.stop_now': 'Stop now', 'action.dismiss': 'Dismiss', 'action.try_again': 'Try again',
    'action.start_meeting': 'Start meeting',

    'where.window': 'this window', 'where.menu_bar': 'the menu bar', 'where.floating': 'the floating button', 'where.spotlight': 'Spotlight',

    'sug.title': 'Call detected in {app}.', 'sug.title_any': 'Call detected.', 'sug.text': 'Start notes for this call?',
    'sug.dismiss': 'Not now',
    'still.title': 'Still in a meeting?', 'still.text': 'Nothing has been said for {n}\u00a0min.',
    'still.ends': 'Without an answer, the meeting ends by itself at {time}.', 'still.keep': 'Keep recording',
    'notice.silence_title': 'The meeting ended by itself.',
    'notice.silence_text': '“{title}” ended after {n}\u00a0min without speech: its notes are ready.',
    'notice.silence_untitled': 'It ended after {n}\u00a0min without speech: its notes are ready.',
    'notice.silence_empty': 'It ended after {n}\u00a0min without speech. Nothing had been said, so there are no notes.',
    'notice.silence_writing': 'It ended after {n}\u00a0min without speech: its notes are being written…',
    'notice.silence_unfinished': 'It ended after {n}\u00a0min without speech.',
    'notice.link_title': 'A link was ignored.',
    'notice.link_text': 'Something asked Voicestream to start recording without its key, so nothing was recorded. To start a meeting, use this window, the menu bar or Spotlight.',

    'top.today': 'Today', 'top.spend_aria': 'Spent today: {amount}. Open Costs.', 'top.spend_title': "Today's spending",
    'cost.stt': 'Speech-to-text', 'cost.claude': 'Claude', 'cost.agents': 'Agents', 'cost.budget_line': 'Daily budget {amount}',

    'ask.stop_title': 'Stop listening?',
    'ask.stop_body': 'The meeting in progress ends now: Voicestream writes its notes and sends the to-dos to your tools.',
    'ask.stop_now_title': 'Stop right away?',
    'ask.stop_now_body': "Voicestream stops without waiting for the notes to be finished. The transcript is kept, but the notes may be incomplete.",
    'ask.apply_title': 'Apply the new settings now?',
    'ask.apply_body': 'This ends the meeting in progress (its notes are written as usual) and starts listening again with the new settings.',
    'ask.end_title': 'End this meeting?',
    'ask.end_body': 'Voicestream writes the notes and sends the to-dos to your tools. It keeps listening for the next meeting.',
    'ask.remove_model_title': 'Remove “{name}”?',
    'ask.remove_model_body': 'This frees {size} on this Mac. You can download it again any time.',

    'notice.restart_pending': 'Your new settings will apply when this meeting ends.',
    'notice.apply_now': 'Apply now (ends the meeting)',

    'hm.mic_off': 'Microphone permission is off: System Settings → Privacy & Security → Microphone → Voicestream',
    'hm.system_off': "Meeting audio isn't coming through: System Settings → Privacy & Security → Screen & System Audio Recording → Voicestream, then reopen Voicestream",
    'hm.stt_key': 'Speech-to-text with {name} needs your {name} key: Settings → Speech-to-text',
    'hm.model_missing': "The speech-to-text model isn't downloaded yet: Settings → Speech-to-text → Download",
    'hm.no_audio': 'Audio capture is missing from this install: reinstall Voicestream',
    'hm.no_whisper': 'Speech-to-text on this Mac is missing from this install: reinstall Voicestream',
    'hm.config': 'Your settings file has a problem: {detail}',
    'hm.stopped': 'Listening stopped unexpectedly (see the log)',
    'hm.no_tools': 'No tools are turned on yet (Settings → Your tools).',
    'hm.model_in_use': 'This model is in use: stop listening or pick another one first.',
    'hm.not_listening': "Voicestream isn't listening right now.",
    'hm.mic_silent': 'The microphone only delivers silence: check the microphone permission for Voicestream.',
    'hm.system_silent': "Meeting audio has been silent for a while: allow Voicestream under Privacy & Security → Screen & System Audio Recording, then restart it.",
    'hm.download_retry': 'The speech model download was interrupted ({detail}): trying again…',
    'hm.download_failed': "The speech model couldn't be downloaded: check your internet connection, then click Download again in Settings → Speech-to-text.",
    'hm.mic_missing_default': "Your microphone ({name}) isn't connected — using {detail} until it is back",
    'hm.mic_missing_only': "Your microphone ({name}) isn't connected — using meeting audio only until it is back",
    'hm.mic_missing_none': "Your microphone ({name}) isn't connected — nothing is recorded until it is back",
    'hm.model_wait_start': "The speech-to-text model is still downloading: listening starts by itself once it's ready (Settings → Speech-to-text shows the progress)",
    'hm.model_wait_switch': "The speech-to-text model is still downloading: Voicestream keeps the current one until then and switches to it once it's ready (Settings → Speech-to-text shows the progress)",
    'hm.not_applied': "Your new settings aren't used yet: {detail}",
    'hm.keychain_denied': "The Keychain didn't let Voicestream read {name}: Voicestream works without it and won't ask again until it is reopened. To use it, paste it again in Settings, or choose “Always Allow” when macOS asks",
    'hm.reminders_denied': 'Voicestream isn’t allowed to add reminders: System Settings → Privacy & Security → Automation → Voicestream → turn on Reminders, then test again.',

    'fix.open_mac_settings': 'Open System Settings', 'fix.stt_settings': 'Speech-to-text settings',
    'fix.tools_settings': 'Set up your tools', 'fix.claude_settings': 'Claude settings', 'fix.show_config': 'Show the settings file',

    'dur.under_minute': 'under a minute', 'dur.minutes': '{n}\u00a0min', 'dur.hours': '{h}\u00a0h\u00a0{m}',  // a figure and its unit never split over two lines
    'size.gb': '{n}\u00a0GB', 'size.mb': '{n}\u00a0MB',

    'kind.action.one': 'To-do', 'kind.action.plural': 'To-dos',
    'kind.decision.one': 'Decision', 'kind.decision.plural': 'Decisions',
    'kind.question.one': 'Question', 'kind.question.plural': 'Open questions',
    'kind.note.one': 'Note', 'kind.note.plural': 'Notes',

    'items.none': 'None yet', 'items.add_ph': 'Add one by hand…', 'items.add': 'Add', 'items.show_dismissed': 'Show dismissed',
    'item.unsure': 'Not sure', 'item.unsure_help': 'Voicestream is not sure this was really said: check it.',
    'item.you': 'You', 'item.due': 'due {when}', 'item.status_done': 'done', 'item.status_cancelled': 'cancelled',
    'item.dismissed': 'dismissed', 'item.evidence': 'What was said', 'item.details': 'Details',
    'item.reopen': 'Reopen', 'item.mark_done': 'Done', 'item.push_again': 'Send again', 'item.push': 'Send',
    'item.push_to': 'Send to {tools}', 'item.no_tools': 'No tools are set up (Settings → Your tools)',
    'item.edit': 'Edit', 'item.restore': 'Restore', 'item.dismiss': 'Dismiss', 'item.title': 'Title', 'item.owner': 'Who',
    'item.due_ph': 'When (e.g. Friday)', 'item.due_label': 'Due', 'item.kind': 'Kind',
    'prio.low': 'low priority', 'prio.medium': 'medium priority', 'prio.high': 'high priority',

    'agent.label': 'Agent', 'agent.run': 'Let an agent do it', 'agent.retry': 'Try again', 'agent.run_anyway': 'Run anyway',
    'agent.run_again': 'Run again', 'agent.reject': 'No thanks', 'agent.you_asked': 'you asked for this',
    'agent.since': 'since {time}', 'agent.brief': 'What the agent should do',
    'agent.brief_help': 'What the agent will be asked to do (change it first if you like):', 'agent.run_now': 'Start',
    'agent.status.suggested': 'suggested', 'agent.status.queued': 'waiting', 'agent.status.running': 'working…',
    'agent.status.done': 'done', 'agent.status.failed': 'failed', 'agent.status.rejected': 'declined',

    'toast.push_failed': "Couldn't send to {tool}", 'toast.pushed': 'Sent', 'toast.pushed_item': '{id} → {tools}',
    'toast.no_tool_takes': 'None of your tools takes this kind of item ({kind}).',
    'toast.meeting_ended.one': '{n} item. The notes are ready.', 'toast.meeting_ended.other': '{n} items. The notes are ready.',
    'toast.meeting_empty': 'Nothing was said, so nothing was kept.', 'toast.meeting_over': 'Meeting ended',
    'toast.notes_updated': 'Notes updated', 'toast.notes_updated_agents': 'The notes now include the agents’ results.',
    'toast.notes_pushed': 'Notes sent', 'toast.agent_budget_title': 'Daily agent budget reached',
    'toast.agent_budget': '{id} was not started automatically. Start it yourself if it’s worth it.',
    'toast.agent_held_title': 'Waiting for your OK',
    'toast.agent_held': '{id} waits for you: the meeting audio is silent, so this “Hey Claude” may have come from the call.',
    'toast.extracted': 'To-dos and decisions are up to date.', 'toast.push_problem': 'Not everything could be sent',
    'toast.pushed_meeting': 'Sent to {tools}.', 'toast.up_to_date': 'Everything was already sent.',
    'toast.rewrite_failed': "Couldn't rewrite the notes", 'toast.rewritten': 'The notes were rewritten.',

    'live.levels': 'Sound levels', 'live.extract': 'Update to-dos now', 'live.extract_short': 'Update now',
    'live.clock_label': 'Meeting time',
    'live.m.stopped': 'No meeting in progress', 'live.m.stopped_text': 'Nothing is recorded until you start one.',
    'live.m.starting': 'Starting the meeting…', 'live.m.recording': 'Recording', 'live.m.recording_text': 'Opening the meeting…',
    'live.m.paused_text': 'Nothing is recorded until you click Resume.',
    'live.m.stopping': 'Wrapping up the meeting…',
    'live.m.stopping_text': 'Writing its notes and sending the to-dos to your tools. The next meeting can start in a moment.',
    'live.m.stopping_queued_text': 'Writing its notes and sending the to-dos to your tools.',
    'live.m.queued': 'The next meeting starts once these notes are written.',
    'live.m.queued_cancel_aria': 'Cancel the next meeting',
    'live.m.error_text': 'See the message above to fix it, then click Try again.',
    'live.mode.manual': 'Meetings start only when you start one, from {where}.',
    'live.mode.automatic': 'Automatic mode: meetings start by themselves when someone speaks.',
    'live.mode.change': 'Change', 'live.mode.change_aria': 'Change how meetings start',
    'live.shortcut.or_press': 'Or press', 'live.shortcut.any_app': 'in any app.',
    'live.shortcut.none': 'No keyboard shortcut yet.',
    'live.shortcut.taken': 'is used by another app: choose another.',
    'live.shortcut.failed': 'was refused by macOS: choose another.',
    'live.extract_help': 'Look through the transcript for new to-dos and decisions right away (it also happens every minute or two).',
    'live.end': 'End meeting', 'live.ending': 'Wrapping up: writing the notes and sending the to-dos to your tools…',
    'live.transcript': 'Transcript', 'live.nothing_yet': 'Nothing said yet.', 'live.jump': 'Jump to the latest',
    'live.items': 'To-dos & decisions', 'live.problem': 'Voicestream needs your attention.',
    'live.more_warnings': 'and {n} more', 'live.untitled': 'Meeting in progress', 'live.mode_claude': 'notes by Claude',
    'live.mode_offline': 'basic offline notes', 'live.started_at': 'started at {time}',
    'live.idle.listening': 'Listening for a meeting to start',
    'live.idle.listening_text': 'A meeting starts by itself as soon as someone speaks, and ends after {n} min of silence. You can close this window: Voicestream keeps listening in the menu bar.',
    'live.idle.listening_text_nosplit': 'A meeting starts by itself as soon as someone speaks. You can close this window: Voicestream keeps listening in the menu bar.',
    'live.idle.paused': 'Paused — nothing is being recorded',
    'live.idle.paused_text': 'Click Resume when you want Voicestream to listen again.',
    'live.idle.starting': 'Getting ready…', 'live.idle.starting_text': 'Starting the microphone and speech-to-text.',
    'live.idle.stopping': 'Wrapping up…', 'live.idle.stopping_text': 'Saving the meeting and writing its notes.',
    'live.idle.error': 'Something needs your attention', 'live.idle.error_text': 'See the message above to fix it, then try again.',
    'live.idle.stopped': 'Not listening', 'live.idle.stopped_text': 'Click Start listening when you want Voicestream to take notes of your meetings.',
    'live.no_meetings': 'No meetings yet', 'live.no_meetings_text': 'Your meetings and their notes will appear here.',
    'live.just_ended': 'Just ended', 'live.last_meeting': 'Last meeting', 'live.open_notes': 'Open the notes',
    'live.week': 'Last 7 days', 'live.week.meetings': 'Meetings', 'live.week.actions': 'To-dos', 'live.week.mine': 'For you',
    'live.week.decisions': 'Decisions', 'live.earlier': 'Before that', 'live.none_earlier': 'No other meeting yet.',
    'live.last_items_none': 'No to-dos or decisions came out of this meeting.', 'live.more_items': 'and {n} more in the notes',
    'live.me': 'Me', 'live.others': 'Others', 'live.screen': 'Screen',
    'voices.title': 'Who spoke when', 'voices.kept': 'Noted', 'voices.share': '{who} {pct}%',
    'src.mic': 'Your microphone', 'src.system': 'Meeting audio',
    'md.done': 'done', 'md.todo': 'to do',

    'meetings.title': 'Meetings', 'meetings.sub': 'Every meeting Voicestream heard, with its notes and to-dos.',
    'meetings.search': 'Search meetings', 'meetings.search_ph': 'Search titles, people, to-dos…',
    'meetings.show_folder': 'Show the folder', 'meetings.live': 'Live now', 'meetings.in_progress': 'In progress',
    'meetings.sent_to': 'Sent to {tools}', 'meetings.error': "Couldn't load the meetings", 'meetings.no_match': 'No meeting matches “{q}”.',
    'meetings.empty': 'No meetings yet', 'meetings.empty_text': 'They appear here as soon as one starts.',
    'meetings.group.today': 'Today', 'meetings.group.yesterday': 'Yesterday', 'meetings.group.week': 'This week',
    'meetings.group.earlier': 'Earlier', 'meetings.more': 'Show more ({n} left)', 'meetings.untitled': 'Untitled meeting',
    'meetings.all': 'All meetings', 'meetings.gone': 'This meeting is no longer there.', 'meetings.with': 'with {names}',
    'meetings.finding.one': '{n} to-do for you is still open in these meetings.',
    'meetings.finding.other': '{n} to-dos for you are still open in these meetings.',
    'meetings.facts': 'This meeting', 'meetings.fact.length': 'Length', 'meetings.fact.language': 'Language',
    'meetings.fact.with': 'With', 'meetings.fact.items': 'To-dos', 'meetings.fact.decisions': 'Decisions',
    'meetings.fact.sent': 'Sent to', 'meetings.fact.not_sent': 'Not sent yet',
    'meetings.push': 'Send to my tools', 'meetings.rewrite': 'Rewrite notes', 'meetings.rewriting': 'Rewriting…',
    'meetings.rewrite_help': 'Write the notes again from the transcript and the current to-dos (takes about a minute).',
    'meetings.finder': 'Show in Finder', 'meetings.folder': 'Show the folder',
    'meetings.readonly': 'This meeting is still being recorded by another copy of Voicestream. You can read it here, but not change it.',
    'meetings.live_here': 'This meeting is in progress.', 'meetings.go_live': 'Follow it live',
    'meetings.interrupted': 'Interrupted',
    'meetings.short_clip': 'Short clip',
    'meetings.short_clip_note': 'Only a few words were heard (a video, a word with a colleague…), so Voicestream kept the transcript and any to-dos but wrote no summary and sent no notes. Use “Rewrite notes” if it was a real meeting.',
    'meetings.interrupted_note': 'Voicestream was closed while it recorded this meeting (a crash, a forced quit or a power cut): it ends at the last words heard and has basic notes. Nothing was sent yet: rewrite the notes and send its to-dos when you are ready.',
    'meetings.sections': 'Meeting sections', 'meetings.tab.notes': 'Notes', 'meetings.tab.actions': 'To-dos & decisions',
    'meetings.tab.transcript': 'Transcript', 'meetings.tab.email': 'Follow-up email',
    'meetings.no_notes': 'No notes yet', 'meetings.no_notes_text': 'Click “Rewrite notes” to write them now.',
    'meetings.no_notes_live': 'They are written when the meeting ends.',
    'meetings.items_help': 'Correct anything that is wrong: the notes and your tools are updated too.',
    'meetings.items_readonly': 'Read only while the meeting is being recorded elsewhere.',
    'meetings.no_transcript': 'No transcript for this meeting.', 'meetings.find': 'Find in the transcript',
    'meetings.find_ph': 'Find in the transcript…', 'meetings.find_none': 'Nothing matches.',
    'meetings.lines.one': '{n} line', 'meetings.lines.other': '{n} lines',
    'count.actions.one': '{n} to-do', 'count.actions.other': '{n} to-dos',
    'count.mine.one': '{n} for you', 'count.mine.other': '{n} for you',
    'count.decisions.one': '{n} decision', 'count.decisions.other': '{n} decisions',
    'count.questions.one': '{n} question', 'count.questions.other': '{n} questions',
    'lang.name.fr': 'French', 'lang.name.en': 'English',

    'email.none': 'No follow-up email', 'email.none_text': 'Claude drafts one when a meeting ends (with a Claude key).',
    'email.to': 'To', 'email.to_none': '(add the recipients in your mail app)', 'email.subject': 'Subject', 'email.body': 'Message',
    'email.copy': 'Copy', 'email.copied': 'The email is on the clipboard.', 'email.open_mail': 'Open in Mail',
    'email.never_sent': 'Nothing is sent until you send it yourself.',

    'costs.title': 'Costs', 'costs.sub': 'What Voicestream spends on online services, in US dollars.',
    'costs.error': "Couldn't load the costs", 'costs.today': 'Today', 'costs.of_budget': '{spent} of your {budget} daily budget',
    'costs.minutes': '{n} min transcribed', 'costs.budget_excludes': 'Agents have their own limit.', 'costs.agents_cap': 'limit {amount} a day', 'costs.budget_link': 'Change the daily budget',
    'costs.bar_aria': '{day}: {total} (speech-to-text {stt}, Claude {claude}, agents {agents})',
    'costs.as_table': 'Show as a table', 'costs.day': 'Day', 'costs.total': 'Total', 'costs.history': 'Last 30 days',
    'costs.history_empty': 'Nothing spent in the last 30 days.',
    'costs.finding': 'Most of it went to {service}: {amount} of the {total} spent in the last 30 days.', 'costs.per_day_usd': 'Per day, in US dollars',
    'costs.key_day': 'Spent that day', 'costs.key_today': 'Today, so far',
    'costs.by_service': 'By service, most costly first', 'costs.month': 'In the last 30 days',
    'costs.per_day_avg': 'About {amount} a day on average',
    'costs.backend.whisper-cpp': 'on this Mac', 'costs.backend.local': 'on this computer', 'costs.backend.openai': 'OpenAI',
    'costs.backend.openai-realtime': 'OpenAI', 'costs.backend.deepgram': 'Deepgram', 'costs.backend.fake': 'test mode',
    'costs.estimate': 'A typical day with your settings', 'costs.hours': '{n} h', 'costs.hours_label': 'Hours of meetings per day',
    'costs.est_stt': 'Speech-to-text ({name})', 'costs.est_extraction': 'To-dos found during meetings',
    'costs.est_summaries': 'Notes written after meetings', 'costs.free': 'free', 'costs.per_day': 'About per day',
    'costs.per_month': 'About per month (20 working days)',
    'costs.explain_free': 'Speech-to-text runs on this Mac: it is free, and your audio never leaves your computer.',
    'costs.explain_cloud': 'Speech-to-text runs at {name}: you pay for each minute someone speaks.',
    'costs.explain_offline': 'Notes are written offline, without Claude: free, but simpler.',
    'costs.explain_claude': 'Claude reads the transcript (text only, never audio) every minute or two to find to-dos, then writes the notes when the meeting ends. Spending stops at your daily budget: notes then continue offline.',
    'costs.explain_agents': 'Agents are extra and only run when you say so (or as set in Settings → Agents), within their own daily limit.',
    'costs.agents_link': 'Agent settings',

    'set.title': 'Settings', 'set.sub': 'Changes apply as soon as you save them (after the meeting, if one is in progress).',
    'set.error': "Couldn't load the settings", 'set.sections': 'Settings sections', 'set.savebar': 'Unsaved changes',
    'set.sec.general': 'General', 'set.sec.stt': 'Speech-to-text', 'set.sec.claude': 'Claude', 'set.sec.audio': 'Audio',
    'set.sec.tools': 'Your tools', 'set.sec.agents': 'Agents', 'set.sec.advanced': 'Advanced',
    'set.unsaved': 'You have unsaved changes.', 'set.save': 'Save changes', 'set.discard': 'Discard',
    'set.saved_title': 'Saved', 'set.saved': 'Your settings are saved.',
    'set.saved_later': 'Saved. They apply when the meeting in progress ends.',
    'set.saved_restarting': 'Saved. Listening restarts with the new settings.',
    'set.saved_after_download': 'Saved. “{name}” is downloading: Voicestream keeps using the current speech-to-text until it is ready, then switches to it.',
    'set.fix_below': 'Nothing was saved: fix the field shown below.',
    'set.you': 'You', 'set.you_lead': 'So Voicestream knows which to-dos are yours.',
    'set.name': 'Your name', 'set.name_ph': 'e.g. Camille Martin',
    'set.name_help': 'When you say “I’ll do it”, the to-do is put in your name.',
    'set.languages': 'Languages spoken in your meetings',
    'set.idle': 'A meeting ends after this much silence', 'set.minutes': 'minutes',
    'set.idle_help': 'The next time someone speaks, a new meeting starts. 0 = never end a meeting by itself.',
    'set.app': 'The app', 'set.app_lead': 'How Voicestream opens, and its language.',
    'start.title': 'Starting meetings', 'start.lead': 'When Voicestream records, and how you start a meeting from any app.',
    'start.how': 'How meetings start',
    'start.manual': 'When you start them (recommended)',
    'start.manual_desc': 'Nothing is recorded until you start a meeting: from this window, the menu bar, the keyboard shortcut, the floating button or Spotlight.',
    'start.automatic': 'Automatically', 'start.automatic_desc': 'Voicestream listens all the time, and a meeting starts as soon as someone speaks.',
    'start.ask': 'Ask “Still in a meeting?” after', 'start.ask_help': 'Keep recording or end the meeting in one click. 0 = never ask.',
    'start.end': 'End the meeting by itself after', 'start.end_help': 'Its notes are written as usual, and Voicestream tells you why it ended. 0 = never.',
    'start.minutes_silence': 'minutes without speech',
    'start.floating': 'Show the floating button',
    'start.floating_help': 'A small button above every app, even in full screen: start, pause and stop a meeting without opening this window. Drag it anywhere; Control-click it to hide it. Hidden, it still comes up to ask about a call or a long silence.',
    'start.spotlight': 'Start a meeting from Spotlight',
    'start.spotlight_help': 'Press ⌘Space, type “voicestream”, then Return. For this, Voicestream keeps a small “Voicestream – New Meeting” app in your Applications folder.',
    'start.suggest': 'Suggest notes when a call starts',
    'start.suggest_help': 'When Zoom, Teams, FaceTime, Meet or Slack starts using the microphone and nothing is recorded, Voicestream offers to start, on the floating button (shown for the question even when it is hidden) and in this window. It never starts by itself.',
    'start.summary': 'Now:', 'start.summary_manual': 'Meetings start when you start them', 'start.summary_auto': 'Meetings start by themselves',
    'start.summary_keys': 'Shortcut {keys}', 'start.summary_floating': 'Floating button', 'start.summary_spotlight': 'Spotlight',
    'start.summary_suggest': 'Suggestions when a call starts',
    'set.ui_language': 'App language', 'set.ui_auto': 'Same as the Mac',
    'set.listen_on_launch': 'Start listening when Voicestream opens',
    'set.listen_on_launch_help': 'Otherwise, click Start listening yourself.',
    'set.start_at_login': 'Open Voicestream when I log in', 'set.login_help': 'Voicestream starts in the menu bar.',
    'set.login_unavailable': 'Only available in the Voicestream app for Mac.',
    'shortcut.label': 'Keyboard shortcut', 'shortcut.choose': 'Choose a shortcut', 'shortcut.turn_off': 'Turn off',
    'shortcut.reset': 'Reset to {keys}', 'shortcut.off': 'Off', 'shortcut.key.space': 'Space',
    'shortcut.press': 'Press the new shortcut…', 'shortcut.press_help': 'Hold ⌘, ⌥ or ⌃ and press a key. Esc cancels.',
    'shortcut.mac_only': 'In the Voicestream app for Mac, a keyboard shortcut starts and stops listening from any app.',
    'shortcut.mac_only_meeting': 'In the Voicestream app for Mac, a keyboard shortcut starts and stops a meeting from any app.',
    'shortcut.status.ok': 'Works from any app, even with this window closed: it starts and stops listening.',
    'shortcut.status.ok_meeting': 'Works from any app, even with this window closed: it starts and stops a meeting.',
    'shortcut.status.taken': 'Already used by another app — choose another.',
    'shortcut.status.failed': 'macOS refused this shortcut (error {code}) — choose another.',
    'shortcut.status.off': 'Turned off. Start and stop listening from the menu bar or this window.',
    'shortcut.status.off_meeting': 'Turned off. Start and stop meetings from the menu bar or this window.',
    'shortcut.why.unknown_key': 'Voicestream can’t use this key for a shortcut: choose a letter, a digit, a function key or a punctuation key.',
    'shortcut.why.needs_modifier': 'Hold ⌘, ⌥ or ⌃ as well: {keys} alone types or does something else.',
    'shortcut.why.one_modifier': '{keys} is taken in many apps already (⌘C copies, ⌥E types an accent, ⌃C stops a Terminal command): hold a second one of ⌘, ⌥ or ⌃, as in ⌥⌘R.',
    'shortcut.why.reserved_app': '{keys} is a command every Mac app has (Quit, Close, Hide, Minimize, Settings): choose another.',
    'shortcut.why.reserved_mac': 'macOS uses {keys} itself (switching apps or windows, Spotlight, input sources, screenshots, Force Quit…): choose another.',
    'shortcut.why.option_only': 'macOS 15 can refuse a shortcut held with ⌥ alone, or ⌥⇧ ({keys}): hold ⌘ or ⌃ too.',
    'shortcut.sound': 'Play a sound when the shortcut starts or stops listening', 'shortcut.sound_help': 'So you know it worked without looking — and another one when listening couldn’t start.',
    'shortcut.sound_meeting': 'Play a sound when the shortcut starts or stops a meeting', 'shortcut.sound_help_meeting': 'So you know it worked without looking, and another one when the meeting couldn’t start.',
    'shortcut.hint': 'You can also press {keys} in any app.',

    'lang.fr+en': 'French and English', 'lang.fr+en.desc': 'Either language, even both in one meeting.',
    'lang.fr': 'French', 'lang.fr.desc': 'Meetings in French only.',
    'lang.en': 'English', 'lang.en.desc': 'Meetings in English only.',
    'lang.any': 'Any language', 'lang.any.desc': 'Detect the language automatically.',
    'lang.other': 'Other: {code}', 'lang.other.desc': 'As set in the settings file.',

    'stt.title': 'Speech-to-text', 'stt.lead': 'What turns the voices into text.',
    'stt.summary': 'In use:', 'stt.summary_model': 'Speech model: {name} · {size}', 'stt.summary_price': 'About {price} per hour of speech',
    'stt.local': 'On this Mac — free & private', 'stt.local_desc': 'The audio never leaves your Mac. Needs a one-time download.',
    'stt.openai': 'OpenAI — online', 'stt.deepgram': 'Deepgram — online',
    'stt.cloud_desc': 'About {price} per hour of speech. Needs a key.',
    'stt.other': 'Other: {name}', 'stt.other_desc': 'As set in the settings file.',
    'stt.models': 'Speech model', 'stt.models_help': 'Bigger models make fewer mistakes but need more memory.',
    'stt.models_dir': 'Saved in {dir}',
    'stt.vad_ok': 'Voice detection helper: downloaded.', 'stt.vad_downloading': 'Voice detection helper: downloading…',
    'stt.vad_missing': 'A small voice detection helper (1 MB) downloads with the model.',
    'stt.cancel': 'Cancel', 'stt.downloaded': 'Downloaded', 'stt.remove': 'Remove', 'stt.remove_named': 'Remove {name}',
    'stt.download_size': 'Download ({size})', 'stt.download_named': 'Download {name}', 'stt.recommended': 'Recommended',
    'stt.size_ram': '{size} · uses about {ram} GB of memory', 'stt.downloading': 'Download progress',
    'stt.progress': '{pct}% · {done} of {total}', 'stt.download_done': '“{name}” is ready.', 'stt.download_failed': "Couldn't download “{name}”",
    'stt.switch_after': '“{name}” is used as soon as it has downloaded. Until then, Voicestream keeps the current speech-to-text.',
    'stt.switched': 'Speech-to-text now uses “{name}”.', 'stt.switched_later': '“{name}” is ready: it is used once the meeting in progress ends.',

    'model.large-v3-turbo-q5_0.label': 'Best for most Macs',
    'model.large-v3-turbo-q5_0.note': 'Near-best accuracy in French and English. Fine on an 8 GB Mac.',
    'model.large-v3-turbo.label': 'Best quality',
    'model.large-v3-turbo.note': 'Slightly more accurate. For Macs with 16 GB of memory or more.',
    'model.small.label': 'Faster', 'model.small.note': 'Quicker on older Macs, but makes more mistakes, especially in French.',
    'model.base.label': 'Lightest', 'model.base.note': 'Very small and fast, noticeably less accurate. For very old Macs.',
    'model.silero-v6.2.0.label': 'Voice detection', 'model.silero-v6.2.0.note': 'Skips noise so nothing is invented.',

    'key.saved_keychain': 'Saved in Keychain', 'key.saved_file': 'Saved on this computer', 'key.not_set': 'Not set',
    'key.kept_keychain': 'Kept in the macOS Keychain, never in a file.', 'key.kept_file': 'Kept in a private file on this computer.',
    'key.paste': 'Paste it here…', 'key.paste_named': 'Paste your {name}', 'key.save': 'Save key',
    'key.saved_toast': '{name} saved.', 'key.replace': 'Replace', 'key.test': 'Test', 'key.remove': 'Remove',
    'key.testing': 'Checking…', 'key.remove_title': 'Remove the {name}?',
    'key.remove_body': 'Voicestream will stop using it until you add it again.',
    'key.claude': 'Claude key', 'key.claude_help': 'Starts with “sk-ant-”.',
    'key.where_claude': 'Where do I get one?',
    'key.openai': 'OpenAI key', 'key.openai_help': 'Starts with “sk-”.', 'key.where_openai': 'Get an OpenAI key',
    'key.deepgram': 'Deepgram key', 'key.deepgram_help': 'From your Deepgram console.', 'key.where_deepgram': 'Get a Deepgram key',

    'perm.title': 'Permissions', 'perm.lead': 'macOS asks once whether Voicestream may hear you and the meeting.',
    'perm.mic': 'Microphone', 'perm.mic_desc': 'What you say.', 'perm.system': 'Meeting audio',
    'perm.system_desc': 'What the others say (Zoom, Teams, Meet…). Play a video while you test.',
    'perm.test_mic': 'Test microphone', 'perm.test_system': 'Test meeting audio', 'perm.testing': 'Listening…',
    'perm.mic_ok': 'Voicestream can hear your microphone.', 'perm.system_ok': 'Voicestream can hear the meeting audio.',
    'perm.mic_denied': "Voicestream can't hear the microphone: allow it in System Settings.",
    'perm.system_denied': "Voicestream can't hear the meeting audio: allow it in System Settings.",
    'perm.mic_unknown': "Couldn't tell whether the microphone works.",
    'perm.system_unknown': 'Nothing was heard. Play some sound and test again.',

    'claude.title': 'Claude', 'claude.lead': 'Claude (by Anthropic) writes the notes and finds the to-dos. It reads text only, never audio.',
    'claude.use': 'Use Claude for notes and to-dos', 'claude.use_help': 'Off: basic notes, written offline, free.',
    'claude.model': 'Notes during meetings', 'claude.model_haiku': 'Fast and cheap (recommended)', 'claude.model_sonnet': 'More thorough (costs more)',
    'claude.model_help': 'The notes written after a meeting always use the more thorough model.',
    'claude.budget_title': 'Daily budget', 'claude.budget_lead': 'A limit for Claude and online speech-to-text, per day.',
    'claude.budget': 'Spend at most', 'claude.per_day': 'US dollars a day',
    'claude.budget_help': 'Once reached, notes continue offline until tomorrow.', 'claude.see_costs': 'See what you spend',

    'audio.title': 'Audio', 'audio.lead': 'What Voicestream listens to.', 'audio.mic': 'Microphone',
    'audio.default_mic': 'The Mac’s default microphone', 'audio.default_suffix': '(default)',
    'audio.mic_help': 'Usually the default is right; pick your headset if you use one.',
    'audio.mic_fallback': 'Use the Mac’s default microphone while this one isn’t connected',
    'audio.mic_fallback_help': 'Off: only the meeting audio is recorded until it is back. Either way Voicestream switches back to it by itself.',
    'audio.no_devices': 'No microphone found ({detail}).',
    'audio.system': 'Also listen to the meeting audio', 'audio.system_help': 'What the others say in Zoom, Teams, Meet… Without it, only your voice is noted.',
    'audio.system_ok': 'Meeting audio: {detail}', 'audio.system_missing': 'Meeting audio is not available: {detail}',
    'audio.store': 'Keep a recording of the audio', 'audio.store_help': 'Saved with each meeting. Off by default: the transcript is usually enough.',

    'tools.lead': 'Where your to-dos and notes go. Turn on the ones you use, then click Test.',
    'tools.more': 'More tools', 'tools.more_help': 'Notion, Linear, Slack…', 'tools.enable': 'Turn on {name}',
    'tools.summary': 'Turned on:', 'tools.summary_none': 'None is turned on yet: the notes stay in your Voicestream folder.',
    'tools.test': 'Test', 'tools.testing': 'Testing…', 'tools.mac_only': 'Mac only',
    'tools.save_first': 'Save your changes first, then test.', 'tools.test_off': 'Turn it on and save first.',
    'tools.test_ok': 'Works: {detail}',
    'tools.reminders.asking': 'Checking Reminders… If macOS asks whether Voicestream may control Reminders, click OK.',
    'tools.only_mine': 'Only my own to-dos', 'tools.only_mine_help': 'Off: everyone’s to-dos.',
    'tools.reminders.list': 'Reminders list', 'tools.reminders.list_help': 'Created if it doesn’t exist.',
    'tools.reminders.time': 'Reminder time', 'tools.reminders.time_help': 'For to-dos with a date, e.g. 09:00. Empty: no time.',
    'tools.markdown.todo': 'To-do list file', 'tools.markdown.todo_ph': 'TODO.md in your Voicestream folder',
    'tools.markdown.todo_help': 'A plain-text list, readable in any app.',
    'tools.markdown.notes': 'Notes folder', 'tools.markdown.notes_ph': 'Each meeting’s own folder',
    'tools.markdown.notes_help': 'Also copy each meeting’s notes here (for example a synced folder).',
    'tools.google.steps_title': 'Connect Google (about 5 minutes, once)',
    'tools.google.step1': 'Open', 'tools.google.step2': 'Delete everything in the editor, paste the bridge script and save.',
    'tools.google.step3': 'Project Settings → Script Properties → add SECRET with a new secret.',
    'tools.google.step4': 'Deploy → New deployment → Web app. Execute as: Me. Who has access: Anyone → Deploy.',
    'tools.google.step5': 'Allow access with your Google account (Advanced → Go to the bridge).',
    'tools.google.step6': 'Copy the Web app URL (it ends with /exec) and paste it below.',
    'tools.google.copy_script': 'Copy the bridge script', 'tools.google.copied': 'The bridge script is on the clipboard.',
    'tools.google.make_secret': 'Make a secret and copy it',
    'tools.google.secret_made': 'A new secret is saved and on the clipboard: paste it as the SECRET property.',
    'tools.google.url': 'Web app URL', 'tools.google.url_help': 'https://script.google.com/macros/s/…/exec',
    'tools.google.secret': 'Secret', 'tools.google.secret_help': 'The same text as the SECRET script property.',
    'tools.google.calendar': 'To-dos in Google Calendar', 'tools.google.calendar_help': 'A reminder on the due date.',
    'tools.google.docs': 'Notes in Google Docs', 'tools.google.docs_help': 'One document per meeting.',
    'tools.google.recap': 'Email me a recap', 'tools.google.recap_help': 'Sent to you only.',
    'tools.google.draft': 'Follow-up email as a Gmail draft', 'tools.google.draft_help': 'Never sent by itself.',
    'tools.hubspot.token': 'HubSpot access token', 'tools.hubspot.token_help': 'From a HubSpot private app (Settings → Integrations → Private apps).',
    'tools.hubspot.owner': 'Your HubSpot email', 'tools.hubspot.owner_help': 'Your to-dos become HubSpot tasks assigned to you.',
    'tools.hubspot.log': 'Log meetings on contacts', 'tools.hubspot.log_help': 'The notes go on the matching contacts.',
    'tools.shortcuts.lead': 'Runs a shortcut from the Shortcuts app for each to-do — for example to add it to Promethee.',
    'tools.shortcuts.item': 'Shortcut for each new to-do', 'tools.shortcuts.item_ph': 'e.g. Add to Promethee',
    'tools.shortcuts.notes': 'Shortcut for the notes', 'tools.shortcuts.update': 'Shortcut when a to-do changes',
    'tools.command.lead': 'Runs a command or opens an app link for each to-do (advanced).',
    'tools.command.item': 'Command for each new to-do', 'tools.command.notes': 'Command for the notes',
    'tools.other_help': 'Its options are in the settings file.',

    'tool.markdown.name': 'Files on this Mac', 'tool.markdown.desc': 'A to-do list and the notes, saved in your Voicestream folder.',
    'tool.reminders.name': 'Apple Reminders', 'tool.reminders.desc': 'Your to-dos in the Reminders app, on all your devices.',
    'tool.google.name': 'Google', 'tool.google.desc': 'Calendar reminders, notes in Docs, a Gmail draft of the follow-up.',
    'tool.hubspot.name': 'HubSpot', 'tool.hubspot.desc': 'Tasks and meeting notes in your CRM.',
    'tool.shortcuts.name': 'Shortcuts (Promethee…)', 'tool.shortcuts.desc': 'Send each to-do to a shortcut.',
    'tool.command.name': 'Command or app link', 'tool.command.desc': 'For apps with a command line or a link.',
    'tool.webhook.name': 'Webhook', 'tool.webhook.desc': 'Zapier, Make, n8n…', 'tool.notion.name': 'Notion', 'tool.notion.desc': '',
    'tool.monday.name': 'monday.com', 'tool.monday.desc': '', 'tool.linear.name': 'Linear', 'tool.linear.desc': '',
    'tool.github.name': 'GitHub Issues', 'tool.github.desc': '', 'tool.slack.name': 'Slack', 'tool.slack.desc': '',
    'tool.todoist.name': 'Todoist', 'tool.todoist.desc': '',

    'agents.title': 'Agents', 'agents.lead': 'An agent is Claude doing a task for you (research, a draft…) while you are still in the meeting.',
    'agents.off': 'Off', 'agents.off.desc': 'Never run agents.',
    'agents.suggest': 'Suggest (recommended)', 'agents.suggest.desc': 'Mark the to-dos an agent could do; it starts only when you click “Let an agent do it”.',
    'agents.commands': 'When I ask', 'agents.commands.desc': 'Also start right away when you say “Hey Claude, …” in a meeting.',
    'agents.auto': 'Automatic', 'agents.auto.desc': 'Start every task an agent can do, within the daily limit.',
    'agents.daily': 'Agents may spend at most', 'agents.daily_help': 'A separate limit from the notes budget.',
    'agents.per_task': 'And at most, per task', 'agents.per_task_help': 'A task that would cost more stops.',
    'agents.per_task_suffix': 'US dollars',

    'adv.files': 'Files', 'adv.files_lead': 'Where Voicestream keeps its settings and your meetings.',
    'adv.config': 'Settings file', 'adv.data': 'Voicestream folder', 'adv.version': 'Version',
    'adv.open_config': 'Show the settings file', 'adv.open_data': 'Show the Voicestream folder', 'adv.open_log': 'Show the log',
    'adv.health': 'Check everything', 'adv.health_lead': 'Keys, microphone, speech-to-text and tools, in one go.',
    'adv.check': 'Run the check', 'adv.checking': 'Checking…',
    'adv.setup': 'Setup', 'adv.setup_lead': 'Go through the first-run setup again.', 'adv.setup_again': 'Run the setup again',

    'setup.title': 'Setup', 'setup.step': 'Step {n} of {total}', 'setup.close': 'Close setup',
    'setup.nav.welcome': 'Welcome', 'setup.nav.you': 'About you', 'setup.nav.stt': 'Speech-to-text', 'setup.nav.claude': 'Claude',
    'setup.nav.perms': 'Permissions', 'setup.nav.tools': 'Your tools', 'setup.nav.start': 'Starting meetings', 'setup.nav.done': 'All set',
    'setup.start.title': 'How you start a meeting',
    'setup.start.lead': 'Choose when Voicestream records. You can change it later in Settings.',
    'setup.start.manual_desc': 'Nothing is recorded until you start a meeting. Start it when the call begins, stop it when it ends.',
    'setup.start.mac_only': 'In the Voicestream app for Mac you can also start a meeting with a keyboard shortcut, from a floating button above every app, from Spotlight, and when a call starts.',
    'setup.done.manual': 'Meetings start when you start them',
    'setup.done.manual_keys': 'Meetings start when you start them, or with {keys} in any app',
    'setup.done.automatic': 'Meetings start by themselves when someone speaks',
    'setup.done.floating': 'A floating button above every app starts and stops meetings',
    'setup.done.spotlight': 'Spotlight starts a meeting: “Voicestream – New Meeting” is in your Applications folder',
    'setup.done.suggest': 'Suggestions when a call starts: Voicestream sees which apps use the microphone',
    'setup.done.finish': 'Done',
    'setup.downloading': 'Downloading the speech model', 'setup.back': 'Back', 'setup.next': 'Continue',
    'setup.welcome.title': 'Welcome to Voicestream',
    'setup.welcome.lead': 'Start a meeting when you want: Voicestream turns it into clear notes and to-do lists, in French and in English.',
    'setup.welcome.p1': 'Private by default', 'setup.welcome.p1_text': 'Recordings and transcripts stay on this Mac. Nothing is shared unless you connect a tool.',
    'setup.welcome.p2': 'You stay in control', 'setup.welcome.p2_text': 'An icon in the menu bar shows when it listens. Pause or stop in one click.',
    'setup.welcome.p3': 'Notes written for you', 'setup.welcome.p3_text': 'Claude reads the transcript (text only, never audio) to write the notes and find the to-dos.',
    'setup.welcome.p4': 'About 3 minutes', 'setup.welcome.p4_text': 'You can change everything later in Settings.',
    'setup.welcome.start': 'Get started',
    'setup.you.title': 'About you', 'setup.you.lead': 'So your to-dos are put in your name.',
    'setup.you.languages': 'Languages spoken in your meetings', 'setup.you.need_name': 'Please enter your name.',
    'setup.stt.title': 'Speech-to-text', 'setup.stt.lead': 'What turns the voices into text. On this Mac is free and private.',
    'setup.stt.local_desc': 'Free, and the audio never leaves your Mac. One download of {size}, starting now.',
    'setup.stt.background': 'The download continues in the background while you finish the setup.',
    'setup.stt.need_key': 'Save your key first, or choose “On this Mac”.',
    'setup.file_problem': 'Your settings file has a problem with {what}, set up outside this window: {detail}',
    'setup.file_problem_file': 'a setting',
    'setup.claude.title': 'Connect Claude', 'setup.claude.lead': 'Claude writes the notes and finds the to-dos. Paste your Claude key from the Anthropic Console.',
    'setup.claude.cost': 'A typical day of meetings costs about $0.10 to $0.50, within a daily budget you control.',
    'setup.claude.privacy': 'Claude receives the transcript text only, never the audio.',
    'setup.claude.need_key': 'Save your key first — or skip: notes are then written offline (simpler).',
    'setup.claude.skip': 'Skip — use offline notes',
    'setup.perms.title': 'Permissions', 'setup.perms.lead': 'Test that Voicestream can hear you and the meeting. macOS may ask for permission the first time.',
    'setup.perms.help_mac': 'If you said no, open System Settings → Privacy & Security and allow Voicestream, then test again.',
    'setup.perms.help_other': 'On this computer, the permissions are managed by the system.',
    'setup.tools.title': 'Your tools', 'setup.tools.lead': 'Where your to-dos go after each meeting.',
    'setup.tools.list_default': 'Meetings',
    'setup.tools.reminders_help': 'Your to-dos appear in the Reminders app, on your iPhone too.',
    'setup.tools.reminders_not_mac': 'Works on a Mac only.',
    'setup.tools.files': 'Always on: a to-do list and the notes are saved in your Voicestream folder.',
    'setup.tools.later': 'Google, HubSpot, Shortcuts (Promethee) and more: set them up later in Settings → Your tools.',
    'setup.tools.check_again': 'Check again',
    'setup.done.title': 'You’re all set, {name}', 'setup.done.lead': 'Here is how Voicestream is set up.',
    'setup.done.lang': 'Meetings in: {lang}', 'setup.done.stt_local': 'Speech-to-text on this Mac — free & private',
    'setup.done.stt_downloading': 'Speech-to-text on this Mac — the model is still downloading',
    'setup.done.stt_missing': 'Speech-to-text on this Mac — download the model in Settings',
    'setup.done.stt_cloud': 'Speech-to-text with {name}', 'setup.done.claude': 'Claude writes your notes',
    'setup.done.offline': 'Offline notes (add a Claude key later in Settings)',
    'setup.done.reminders': 'To-dos go to Apple Reminders (“{list}” list)',
    'setup.done.files': 'Notes and to-dos are saved in your Voicestream folder',
    'setup.done.start': 'Start listening', 'setup.done.start_after': 'Start listening when the download is done',
    'setup.done.later': 'Not now', 'setup.done.will_start': 'Voicestream starts listening as soon as the download is done.',
  },

  fr: {
    'boot.opening': 'Ouverture de Voicestream…',
    'boot.failed': 'Voicestream n’a pas pu démarrer : {detail}',

    'common.close': 'Fermer', 'common.ok': 'OK', 'common.cancel': 'Annuler', 'common.save': 'Enregistrer', 'common.open': 'Ouvrir',
    'common.loading': 'Chargement…', 'common.skip': 'Aller au contenu', 'common.hide': 'Masquer',

    'err.unreachable': 'Impossible de joindre Voicestream : vérifiez qu’il est toujours ouvert, puis réessayez.',
    'err.minutes': 'Saisissez un nombre entier de minutes, de 0 à 600.',
    'err.end_before_ask': 'Choisissez plus de minutes que pour « Toujours en réunion ? » ({n}), sinon la question n’est jamais posée. Pour ne jamais la poser, mettez-y 0.',
    'err.copy': 'Impossible de copier dans le presse-papiers.',
    'err.required': 'Ce champ ne peut pas être vide.',
    'err.number': 'Saisissez un nombre.',
    'err.format': 'Le format ne convient pas — voyez l’exemple sous le champ.',
    'err.shortcuts': 'Indiquez le nom d’au moins un raccourci.',
    'err.command': 'Indiquez au moins une commande.',
    'err.range': 'Cette valeur est hors limites ({detail}).',
    'file.cannot_open': 'Impossible d’ouvrir {name}',

    'conn.connecting': 'Connexion…', 'conn.live': 'Connecté', 'conn.retry': 'Reconnexion…', 'conn.dead': 'Non connecté',
    'conn.auth': 'Cette page doit être ouverte depuis Voicestream : son lien contient une clé qui change à chaque démarrage. Rouvrez Voicestream.',
    'conn.lost': 'Connexion à Voicestream perdue. Nouvel essai…',

    'nav.label': 'Navigation', 'nav.live': 'En direct', 'nav.meetings': 'Réunions', 'nav.costs': 'Coûts', 'nav.settings': 'Réglages',
    'nav.live_now': 'En cours',
    'nav.menu': 'Menu',

    'state.stopped': 'À l’arrêt', 'state.starting': 'Démarrage…', 'state.listening': 'À l’écoute',
    'state.paused': 'En pause — rien n’est enregistré', 'state.paused_short': 'En pause', 'state.stopping': 'Arrêt…',
    'state.error': 'Action requise', 'state.meeting_for': 'Réunion en cours · {time}', 'state.in_meeting': 'Une réunion est en cours',
    'state.m.stopped': 'Pas d’enregistrement', 'state.m.listening': 'Enregistrement', 'state.m.stopping': 'Finalisation…',

    'action.start': 'Commencer l’écoute', 'action.pause': 'Pause', 'action.resume': 'Reprendre', 'action.stop': 'Arrêter',
    'action.stop_now': 'Arrêter tout de suite', 'action.dismiss': 'Ignorer', 'action.try_again': 'Réessayer',
    'action.start_meeting': 'Démarrer une réunion',

    'where.window': 'cette fenêtre', 'where.menu_bar': 'la barre des menus', 'where.floating': 'le bouton flottant', 'where.spotlight': 'Spotlight',

    'sug.title': 'Appel détecté dans {app}.', 'sug.title_any': 'Appel détecté.', 'sug.text': 'Prendre des notes de cet appel ?',
    'sug.dismiss': 'Pas maintenant',
    'still.title': 'Toujours en réunion ?', 'still.text': 'Rien n’a été dit depuis {n}\u00a0min.',
    'still.ends': 'Sans réponse, la réunion se termine d’elle-même à {time}.', 'still.keep': 'Continuer l’enregistrement',
    'notice.silence_title': 'La réunion s’est terminée d’elle-même.',
    'notice.silence_text': '« {title} » s’est terminée après {n}\u00a0min sans parole : ses notes sont prêtes.',
    'notice.silence_untitled': 'Elle s’est terminée après {n}\u00a0min sans parole : ses notes sont prêtes.',
    'notice.silence_empty': 'Elle s’est terminée après {n}\u00a0min sans parole. Rien n’avait été dit, il n’y a donc pas de notes.',
    'notice.silence_writing': 'Elle s’est terminée après {n}\u00a0min sans parole : ses notes sont en cours de rédaction…',
    'notice.silence_unfinished': 'Elle s’est terminée après {n}\u00a0min sans parole.',
    'notice.link_title': 'Un lien a été ignoré.',
    'notice.link_text': 'Quelque chose a demandé à Voicestream d’enregistrer sans sa clé : rien n’a été enregistré. Pour démarrer une réunion, utilisez cette fenêtre, la barre des menus ou Spotlight.',

    'top.today': 'Aujourd’hui', 'top.spend_aria': 'Dépensé aujourd’hui : {amount}. Ouvrir les coûts.', 'top.spend_title': 'Dépenses du jour',
    'cost.stt': 'Transcription', 'cost.claude': 'Claude', 'cost.agents': 'Agents', 'cost.budget_line': 'Budget du jour {amount}',

    'ask.stop_title': 'Arrêter l’écoute ?',
    'ask.stop_body': 'La réunion en cours se termine maintenant : Voicestream rédige ses notes et envoie les tâches vers vos outils.',
    'ask.stop_now_title': 'Arrêter tout de suite ?',
    'ask.stop_now_body': 'Voicestream s’arrête sans attendre la fin des notes. La transcription est conservée, mais les notes peuvent être incomplètes.',
    'ask.apply_title': 'Appliquer les nouveaux réglages maintenant ?',
    'ask.apply_body': 'La réunion en cours se termine (ses notes sont rédigées normalement) et l’écoute reprend avec les nouveaux réglages.',
    'ask.end_title': 'Terminer cette réunion ?',
    'ask.end_body': 'Voicestream rédige les notes et envoie les tâches vers vos outils. Il reste à l’écoute de la réunion suivante.',
    'ask.remove_model_title': 'Supprimer « {name} » ?',
    'ask.remove_model_body': 'Cela libère {size} sur ce Mac. Vous pourrez le retélécharger à tout moment.',

    'notice.restart_pending': 'Vos nouveaux réglages s’appliqueront à la fin de cette réunion.',
    'notice.apply_now': 'Appliquer maintenant (termine la réunion)',

    'hm.mic_off': 'L’accès au micro est désactivé : Réglages Système → Confidentialité et sécurité → Microphone → Voicestream',
    'hm.system_off': 'Le son de la réunion n’arrive pas : Réglages Système → Confidentialité et sécurité → Enregistrement de l’écran et de l’audio système → Voicestream, puis rouvrez Voicestream',
    'hm.stt_key': 'La transcription avec {name} demande votre clé {name} : Réglages → Transcription',
    'hm.model_missing': 'Le modèle de transcription n’est pas encore téléchargé : Réglages → Transcription → Télécharger',
    'hm.no_audio': 'La capture audio manque dans cette installation : réinstallez Voicestream',
    'hm.no_whisper': 'La transcription sur ce Mac manque dans cette installation : réinstallez Voicestream',
    'hm.config': 'Votre fichier de réglages a un problème : {detail}',
    'hm.stopped': 'L’écoute s’est arrêtée de façon inattendue (voir le journal)',
    'hm.no_tools': 'Aucun outil n’est activé (Réglages → Vos outils).',
    'hm.model_in_use': 'Ce modèle est utilisé : arrêtez l’écoute ou choisissez-en un autre d’abord.',
    'hm.not_listening': 'Voicestream n’est pas à l’écoute en ce moment.',
    'hm.mic_silent': 'Le micro ne transmet que du silence : vérifiez l’autorisation du micro pour Voicestream.',
    'hm.system_silent': 'Le son de la réunion est muet depuis un moment : autorisez Voicestream dans Confidentialité et sécurité → Enregistrement de l’écran et de l’audio système, puis redémarrez-le.',
    'hm.download_retry': 'Le téléchargement du modèle de transcription a été interrompu ({detail}) : nouvel essai…',
    'hm.download_failed': 'Le modèle de transcription n’a pas pu être téléchargé : vérifiez votre connexion, puis cliquez à nouveau sur Télécharger dans Réglages → Transcription.',
    'hm.mic_missing_default': 'Votre micro ({name}) n’est pas branché : Voicestream utilise {detail} en attendant qu’il revienne',
    'hm.mic_missing_only': 'Votre micro ({name}) n’est pas branché : seul le son de la réunion est enregistré en attendant qu’il revienne',
    'hm.mic_missing_none': 'Votre micro ({name}) n’est pas branché : rien n’est enregistré en attendant qu’il revienne',
    'hm.model_wait_start': 'Le modèle de transcription se télécharge encore : l’écoute démarrera d’elle-même dès qu’il sera prêt (Réglages → Transcription montre la progression)',
    'hm.model_wait_switch': 'Le modèle de transcription se télécharge encore : Voicestream garde l’actuel et passera au nouveau dès qu’il sera prêt (Réglages → Transcription montre la progression)',
    'hm.not_applied': 'Vos nouveaux réglages ne sont pas encore utilisés : {detail}',
    'hm.keychain_denied': 'Le trousseau n’a pas laissé Voicestream lire {name} : Voicestream fonctionne sans et ne redemandera pas avant d’être rouvert. Pour l’utiliser, collez-la à nouveau dans les Réglages, ou choisissez « Toujours autoriser » quand macOS le demande',
    'hm.reminders_denied': 'Voicestream n’a pas le droit d’ajouter des rappels : Réglages Système → Confidentialité et sécurité → Automatisation → Voicestream → activez Rappels, puis testez à nouveau.',

    'fix.open_mac_settings': 'Ouvrir les Réglages Système', 'fix.stt_settings': 'Réglages de transcription',
    'fix.tools_settings': 'Configurer vos outils', 'fix.claude_settings': 'Réglages de Claude', 'fix.show_config': 'Afficher le fichier de réglages',

    'dur.under_minute': 'moins d’une minute', 'dur.minutes': '{n}\u00a0min', 'dur.hours': '{h}\u00a0h\u00a0{m}',
    'size.gb': '{n}\u00a0Go', 'size.mb': '{n}\u00a0Mo',

    'kind.action.one': 'Tâche', 'kind.action.plural': 'Tâches',
    'kind.decision.one': 'Décision', 'kind.decision.plural': 'Décisions',
    'kind.question.one': 'Question', 'kind.question.plural': 'Questions ouvertes',
    'kind.note.one': 'Note', 'kind.note.plural': 'Notes',

    'items.none': 'Rien pour l’instant', 'items.add_ph': 'Ajouter à la main…', 'items.add': 'Ajouter', 'items.show_dismissed': 'Voir les écartés',
    'item.unsure': 'Incertain', 'item.unsure_help': 'Voicestream n’est pas sûr que cela ait vraiment été dit : vérifiez.',
    'item.you': 'Vous', 'item.due': 'pour {when}', 'item.status_done': 'fait', 'item.status_cancelled': 'annulé',
    'item.dismissed': 'écarté', 'item.evidence': 'Ce qui a été dit', 'item.details': 'Détails',
    'item.reopen': 'Rouvrir', 'item.mark_done': 'Fait', 'item.push_again': 'Renvoyer', 'item.push': 'Envoyer',
    'item.push_to': 'Envoyer vers {tools}', 'item.no_tools': 'Aucun outil configuré (Réglages → Vos outils)',
    'item.edit': 'Modifier', 'item.restore': 'Rétablir', 'item.dismiss': 'Écarter', 'item.title': 'Intitulé', 'item.owner': 'Qui',
    'item.due_ph': 'Quand (ex. vendredi)', 'item.due_label': 'Échéance', 'item.kind': 'Type',
    'prio.low': 'priorité basse', 'prio.medium': 'priorité moyenne', 'prio.high': 'priorité haute',

    'agent.label': 'Agent', 'agent.run': 'Confier à un agent', 'agent.retry': 'Réessayer', 'agent.run_anyway': 'Lancer quand même',
    'agent.run_again': 'Relancer', 'agent.reject': 'Non merci', 'agent.you_asked': 'vous l’avez demandé',
    'agent.since': 'depuis {time}', 'agent.brief': 'Ce que l’agent doit faire',
    'agent.brief_help': 'Ce qui sera demandé à l’agent (modifiez-le si vous voulez) :', 'agent.run_now': 'Lancer',
    'agent.status.suggested': 'suggéré', 'agent.status.queued': 'en attente', 'agent.status.running': 'au travail…',
    'agent.status.done': 'terminé', 'agent.status.failed': 'échec', 'agent.status.rejected': 'refusé',

    'toast.push_failed': 'Envoi vers {tool} impossible', 'toast.pushed': 'Envoyé', 'toast.pushed_item': '{id} → {tools}',
    'toast.no_tool_takes': 'Aucun de vos outils ne reçoit ce type d’élément ({kind}).',
    'toast.meeting_ended.one': '{n} élément. Les notes sont prêtes.', 'toast.meeting_ended.other': '{n} éléments. Les notes sont prêtes.',
    'toast.meeting_empty': 'Rien n’a été dit : rien n’a été gardé.', 'toast.meeting_over': 'Réunion terminée',
    'toast.notes_updated': 'Notes mises à jour', 'toast.notes_updated_agents': 'Les notes incluent maintenant le travail des agents.',
    'toast.notes_pushed': 'Notes envoyées', 'toast.agent_budget_title': 'Budget quotidien des agents atteint',
    'toast.agent_budget': '{id} n’a pas été lancé automatiquement. Lancez-le vous-même si cela en vaut la peine.',
    'toast.agent_held_title': 'En attente de votre accord',
    'toast.agent_held': '{id} vous attend : le son de la réunion est muet, ce « Hey Claude » vient peut-être de l’appel.',
    'toast.extracted': 'Les tâches et décisions sont à jour.', 'toast.push_problem': 'Tout n’a pas pu être envoyé',
    'toast.pushed_meeting': 'Envoyé vers {tools}.', 'toast.up_to_date': 'Tout avait déjà été envoyé.',
    'toast.rewrite_failed': 'Impossible de réécrire les notes', 'toast.rewritten': 'Les notes ont été réécrites.',

    'live.levels': 'Niveaux sonores', 'live.extract': 'Mettre à jour les tâches', 'live.extract_short': 'Mettre à jour',
    'live.clock_label': 'Durée de la réunion',
    'live.m.stopped': 'Aucune réunion en cours', 'live.m.stopped_text': 'Rien n’est enregistré tant que vous n’en démarrez pas une.',
    'live.m.starting': 'Démarrage de la réunion…', 'live.m.recording': 'Enregistrement', 'live.m.recording_text': 'Ouverture de la réunion…',
    'live.m.paused_text': 'Rien n’est enregistré tant que vous ne cliquez pas sur Reprendre.',
    'live.m.stopping': 'Finalisation de la réunion…',
    'live.m.stopping_text': 'Rédaction des notes et envoi des tâches vers vos outils. La réunion suivante pourra démarrer dans un instant.',
    'live.m.stopping_queued_text': 'Rédaction des notes et envoi des tâches vers vos outils.',
    'live.m.queued': 'La réunion suivante démarrera une fois ces notes rédigées.',
    'live.m.queued_cancel_aria': 'Annuler la prochaine réunion',
    'live.m.error_text': 'Suivez le message ci-dessus pour corriger, puis cliquez sur Réessayer.',
    'live.mode.manual': 'Une réunion ne démarre que quand vous la lancez, depuis {where}.',
    'live.mode.automatic': 'Mode automatique : les réunions démarrent d’elles-mêmes dès que quelqu’un parle.',
    'live.mode.change': 'Modifier', 'live.mode.change_aria': 'Modifier la façon dont les réunions démarrent',
    'live.shortcut.or_press': 'Ou appuyez sur', 'live.shortcut.any_app': 'dans n’importe quelle app.',
    'live.shortcut.none': 'Pas encore de raccourci clavier.',
    'live.shortcut.taken': 'est déjà utilisé par une autre app : choisissez-en un autre.',
    'live.shortcut.failed': 'a été refusé par macOS : choisissez-en un autre.',
    'live.extract_help': 'Chercher tout de suite les nouvelles tâches et décisions dans la transcription (cela se fait aussi toutes les une à deux minutes).',
    'live.end': 'Terminer la réunion', 'live.ending': 'Finalisation : rédaction des notes et envoi des tâches vers vos outils…',
    'live.transcript': 'Transcription', 'live.nothing_yet': 'Rien n’a encore été dit.', 'live.jump': 'Revenir au direct',
    'live.items': 'Tâches et décisions', 'live.problem': 'Voicestream a besoin de vous.',
    'live.more_warnings': 'et {n} autres', 'live.untitled': 'Réunion en cours', 'live.mode_claude': 'notes par Claude',
    'live.mode_offline': 'notes simples hors ligne', 'live.started_at': 'commencée à {time}',
    'live.idle.listening': 'À l’écoute du début d’une réunion',
    'live.idle.listening_text': 'Une réunion commence d’elle-même dès que quelqu’un parle, et se termine après {n} min de silence. Vous pouvez fermer cette fenêtre : Voicestream reste à l’écoute dans la barre des menus.',
    'live.idle.listening_text_nosplit': 'Une réunion commence d’elle-même dès que quelqu’un parle. Vous pouvez fermer cette fenêtre : Voicestream reste à l’écoute dans la barre des menus.',
    'live.idle.paused': 'En pause — rien n’est enregistré',
    'live.idle.paused_text': 'Cliquez sur Reprendre quand vous voulez que Voicestream écoute à nouveau.',
    'live.idle.starting': 'Préparation…', 'live.idle.starting_text': 'Démarrage du micro et de la transcription.',
    'live.idle.stopping': 'Finalisation…', 'live.idle.stopping_text': 'Enregistrement de la réunion et rédaction de ses notes.',
    'live.idle.error': 'Quelque chose demande votre attention', 'live.idle.error_text': 'Suivez le message ci-dessus pour corriger, puis réessayez.',
    'live.idle.stopped': 'À l’arrêt', 'live.idle.stopped_text': 'Cliquez sur Commencer l’écoute quand vous voulez que Voicestream prenne des notes de vos réunions.',
    'live.no_meetings': 'Aucune réunion pour l’instant', 'live.no_meetings_text': 'Vos réunions et leurs notes apparaîtront ici.',
    'live.just_ended': 'Vient de se terminer', 'live.last_meeting': 'Dernière réunion', 'live.open_notes': 'Ouvrir les notes',
    'live.week': '7 derniers jours', 'live.week.meetings': 'Réunions', 'live.week.actions': 'Tâches', 'live.week.mine': 'Pour vous',
    'live.week.decisions': 'Décisions', 'live.earlier': 'Avant', 'live.none_earlier': 'Pas d’autre réunion pour l’instant.',
    'live.last_items_none': 'Aucune tâche ni décision n’est sortie de cette réunion.', 'live.more_items': 'et {n} autres dans les notes',
    'live.me': 'Moi', 'live.others': 'Les autres', 'live.screen': 'Écran',
    'voices.title': 'Qui a parlé, et quand', 'voices.kept': 'Noté', 'voices.share': '{who} {pct} %',
    'src.mic': 'Votre micro', 'src.system': 'Son de la réunion',
    'md.done': 'fait', 'md.todo': 'à faire',

    'meetings.title': 'Réunions', 'meetings.sub': 'Toutes les réunions entendues par Voicestream, avec leurs notes et leurs tâches.',
    'meetings.search': 'Rechercher des réunions', 'meetings.search_ph': 'Titres, personnes, tâches…',
    'meetings.show_folder': 'Afficher le dossier', 'meetings.live': 'En direct', 'meetings.in_progress': 'En cours',
    'meetings.sent_to': 'Envoyé vers {tools}', 'meetings.error': 'Impossible de charger les réunions', 'meetings.no_match': 'Aucune réunion ne correspond à « {q} ».',
    'meetings.empty': 'Aucune réunion pour l’instant', 'meetings.empty_text': 'Elles apparaissent ici dès que l’une commence.',
    'meetings.group.today': 'Aujourd’hui', 'meetings.group.yesterday': 'Hier', 'meetings.group.week': 'Cette semaine',
    'meetings.group.earlier': 'Plus tôt', 'meetings.more': 'Voir plus ({n} restantes)', 'meetings.untitled': 'Réunion sans titre',
    'meetings.all': 'Toutes les réunions', 'meetings.gone': 'Cette réunion n’existe plus.', 'meetings.with': 'avec {names}',
    'meetings.finding.one': '{n} tâche pour vous est encore ouverte dans ces réunions.',
    'meetings.finding.other': '{n} tâches pour vous sont encore ouvertes dans ces réunions.',
    'meetings.facts': 'Cette réunion', 'meetings.fact.length': 'Durée', 'meetings.fact.language': 'Langue',
    'meetings.fact.with': 'Avec', 'meetings.fact.items': 'Tâches', 'meetings.fact.decisions': 'Décisions',
    'meetings.fact.sent': 'Envoyé vers', 'meetings.fact.not_sent': 'Pas encore envoyé',
    'meetings.push': 'Envoyer vers mes outils', 'meetings.rewrite': 'Réécrire les notes', 'meetings.rewriting': 'Réécriture…',
    'meetings.rewrite_help': 'Rédiger à nouveau les notes à partir de la transcription et des tâches actuelles (environ une minute).',
    'meetings.finder': 'Afficher dans le Finder', 'meetings.folder': 'Afficher le dossier',
    'meetings.readonly': 'Cette réunion est encore enregistrée par un autre Voicestream. Vous pouvez la lire ici, mais pas la modifier.',
    'meetings.live_here': 'Cette réunion est en cours.', 'meetings.go_live': 'La suivre en direct',
    'meetings.interrupted': 'Interrompue',
    'meetings.short_clip': 'Extrait court',
    'meetings.short_clip_note': 'Seuls quelques mots ont été entendus (une vidéo, un échange rapide…) : Voicestream a gardé la transcription et les éventuelles tâches, sans écrire de résumé ni envoyer de notes. Utilisez « Réécrire les notes » si c’était une vraie réunion.',
    'meetings.interrupted_note': 'Voicestream a été fermé pendant l’enregistrement de cette réunion (plantage, arrêt forcé ou coupure de courant) : elle s’arrête aux derniers mots entendus et ses notes sont sommaires. Rien n’a encore été envoyé : réécrivez les notes et envoyez ses tâches quand vous voulez.',
    'meetings.sections': 'Sections de la réunion', 'meetings.tab.notes': 'Notes', 'meetings.tab.actions': 'Tâches et décisions',
    'meetings.tab.transcript': 'Transcription', 'meetings.tab.email': 'E-mail de suivi',
    'meetings.no_notes': 'Pas encore de notes', 'meetings.no_notes_text': 'Cliquez sur « Réécrire les notes » pour les rédiger maintenant.',
    'meetings.no_notes_live': 'Elles sont rédigées à la fin de la réunion.',
    'meetings.items_help': 'Corrigez ce qui est faux : les notes et vos outils sont mis à jour aussi.',
    'meetings.items_readonly': 'Lecture seule tant que la réunion est enregistrée ailleurs.',
    'meetings.no_transcript': 'Pas de transcription pour cette réunion.', 'meetings.find': 'Chercher dans la transcription',
    'meetings.find_ph': 'Chercher dans la transcription…', 'meetings.find_none': 'Aucun résultat.',
    'meetings.lines.one': '{n} ligne', 'meetings.lines.other': '{n} lignes',
    'count.actions.one': '{n} tâche', 'count.actions.other': '{n} tâches',
    'count.mine.one': '{n} pour vous', 'count.mine.other': '{n} pour vous',
    'count.decisions.one': '{n} décision', 'count.decisions.other': '{n} décisions',
    'count.questions.one': '{n} question', 'count.questions.other': '{n} questions',
    'lang.name.fr': 'Français', 'lang.name.en': 'Anglais',

    'email.none': 'Pas d’e-mail de suivi', 'email.none_text': 'Claude en rédige un à la fin de chaque réunion (avec une clé Claude).',
    'email.to': 'À', 'email.to_none': '(ajoutez les destinataires dans votre messagerie)', 'email.subject': 'Objet', 'email.body': 'Message',
    'email.copy': 'Copier', 'email.copied': 'L’e-mail est dans le presse-papiers.', 'email.open_mail': 'Ouvrir dans Mail',
    'email.never_sent': 'Rien n’est envoyé tant que vous ne l’envoyez pas vous-même.',

    'costs.title': 'Coûts', 'costs.sub': 'Ce que Voicestream dépense en services en ligne, en dollars américains.',
    'costs.error': 'Impossible de charger les coûts', 'costs.today': 'Aujourd’hui', 'costs.of_budget': '{spent} sur votre budget de {budget} par jour',
    'costs.minutes': '{n} min transcrites', 'costs.budget_excludes': 'Les agents ont leur propre limite.', 'costs.agents_cap': 'limite {amount} par jour', 'costs.budget_link': 'Modifier le budget quotidien',
    'costs.bar_aria': '{day} : {total} (transcription {stt}, Claude {claude}, agents {agents})',
    'costs.as_table': 'Afficher en tableau', 'costs.day': 'Jour', 'costs.total': 'Total', 'costs.history': '30 derniers jours',
    'costs.history_empty': 'Aucune dépense ces 30 derniers jours.',
    'costs.finding': 'La plus grande part pour {service} : {amount} des {total} dépensés ces 30 derniers jours.', 'costs.per_day_usd': 'Par jour, en dollars US',
    'costs.key_day': 'Dépensé ce jour-là', 'costs.key_today': 'Aujourd’hui, pour l’instant',
    'costs.by_service': 'Par service, le plus coûteux d’abord', 'costs.month': 'Ces 30 derniers jours',
    'costs.per_day_avg': 'Environ {amount} par jour en moyenne',
    'costs.backend.whisper-cpp': 'sur ce Mac', 'costs.backend.local': 'sur cet ordinateur', 'costs.backend.openai': 'OpenAI',
    'costs.backend.openai-realtime': 'OpenAI', 'costs.backend.deepgram': 'Deepgram', 'costs.backend.fake': 'mode test',
    'costs.estimate': 'Une journée type avec vos réglages', 'costs.hours': '{n} h', 'costs.hours_label': 'Heures de réunion par jour',
    'costs.est_stt': 'Transcription ({name})', 'costs.est_extraction': 'Tâches repérées pendant les réunions',
    'costs.est_summaries': 'Notes rédigées après les réunions', 'costs.free': 'gratuit', 'costs.per_day': 'Environ par jour',
    'costs.per_month': 'Environ par mois (20 jours ouvrés)',
    'costs.explain_free': 'La transcription se fait sur ce Mac : elle est gratuite, et votre audio ne quitte jamais votre ordinateur.',
    'costs.explain_cloud': 'La transcription se fait chez {name} : vous payez chaque minute de parole.',
    'costs.explain_offline': 'Les notes sont rédigées hors ligne, sans Claude : gratuit, mais plus simple.',
    'costs.explain_claude': 'Claude lit la transcription (le texte seulement, jamais l’audio) toutes les une à deux minutes pour repérer les tâches, puis rédige les notes à la fin de la réunion. Les dépenses s’arrêtent à votre budget quotidien : les notes continuent alors hors ligne.',
    'costs.explain_agents': 'Les agents sont en plus et ne travaillent que si vous le demandez (ou selon Réglages → Agents), dans leur propre limite quotidienne.',
    'costs.agents_link': 'Réglages des agents',

    'set.title': 'Réglages', 'set.sub': 'Les changements s’appliquent dès que vous les enregistrez (après la réunion, s’il y en a une en cours).',
    'set.error': 'Impossible de charger les réglages', 'set.sections': 'Sections des réglages', 'set.savebar': 'Modifications non enregistrées',
    'set.sec.general': 'Général', 'set.sec.stt': 'Transcription', 'set.sec.claude': 'Claude', 'set.sec.audio': 'Audio',
    'set.sec.tools': 'Vos outils', 'set.sec.agents': 'Agents', 'set.sec.advanced': 'Avancé',
    'set.unsaved': 'Vous avez des modifications non enregistrées.', 'set.save': 'Enregistrer', 'set.discard': 'Annuler les modifications',
    'set.saved_title': 'Enregistré', 'set.saved': 'Vos réglages sont enregistrés.',
    'set.saved_later': 'Enregistré. Ils s’appliqueront à la fin de la réunion en cours.',
    'set.saved_restarting': 'Enregistré. L’écoute redémarre avec les nouveaux réglages.',
    'set.saved_after_download': 'Enregistré. « {name} » se télécharge : Voicestream garde la transcription actuelle jusqu’à ce qu’il soit prêt, puis passe dessus.',
    'set.fix_below': 'Rien n’a été enregistré : corrigez le champ indiqué ci-dessous.',
    'set.you': 'Vous', 'set.you_lead': 'Pour que Voicestream sache quelles tâches sont les vôtres.',
    'set.name': 'Votre nom', 'set.name_ph': 'ex. Camille Martin',
    'set.name_help': 'Quand vous dites « je m’en occupe », la tâche est mise à votre nom.',
    'set.languages': 'Langues parlées en réunion',
    'set.idle': 'Une réunion se termine après ce silence', 'set.minutes': 'minutes',
    'set.idle_help': 'Dès que quelqu’un reparle, une nouvelle réunion commence. 0 = ne jamais terminer une réunion d’elle-même.',
    'set.app': 'L’application', 'set.app_lead': 'Comment Voicestream s’ouvre, et sa langue.',
    'start.title': 'Démarrer une réunion', 'start.lead': 'Quand Voicestream enregistre, et comment démarrer une réunion depuis n’importe quelle app.',
    'start.how': 'Comment les réunions démarrent',
    'start.manual': 'Quand vous les démarrez (recommandé)',
    'start.manual_desc': 'Rien n’est enregistré tant que vous ne démarrez pas une réunion : depuis cette fenêtre, la barre des menus, le raccourci clavier, le bouton flottant ou Spotlight.',
    'start.automatic': 'Automatiquement', 'start.automatic_desc': 'Voicestream écoute en permanence, et une réunion démarre dès que quelqu’un parle.',
    'start.ask': 'Demander « Toujours en réunion ? » après', 'start.ask_help': 'Continuer l’enregistrement ou terminer la réunion en un clic. 0 = ne jamais demander.',
    'start.end': 'Terminer la réunion d’elle-même après', 'start.end_help': 'Ses notes sont rédigées comme d’habitude, et Voicestream vous dit pourquoi elle s’est terminée. 0 = jamais.',
    'start.minutes_silence': 'minutes sans parole',
    'start.floating': 'Afficher le bouton flottant',
    'start.floating_help': 'Un petit bouton au-dessus de toutes les apps, même en plein écran : démarrez, mettez en pause et arrêtez une réunion sans ouvrir cette fenêtre. Déplacez-le où vous voulez ; Contrôle-clic pour le masquer. Masqué, il réapparaît quand même pour une question : un appel, un long silence.',
    'start.spotlight': 'Démarrer une réunion depuis Spotlight',
    'start.spotlight_help': 'Appuyez sur ⌘Espace, tapez « voicestream », puis Retour. Pour cela, Voicestream garde une petite app « Voicestream – Nouvelle réunion » dans votre dossier Applications.',
    'start.suggest': 'Proposer des notes quand un appel commence',
    'start.suggest_help': 'Quand Zoom, Teams, FaceTime, Meet ou Slack se met à utiliser le micro et que rien n’est enregistré, Voicestream propose de démarrer, sur le bouton flottant (affiché pour la question même s’il est masqué) et dans cette fenêtre. Il ne démarre jamais tout seul.',
    'start.summary': 'Actuellement :', 'start.summary_manual': 'Les réunions démarrent quand vous les lancez', 'start.summary_auto': 'Les réunions démarrent d’elles-mêmes',
    'start.summary_keys': 'Raccourci {keys}', 'start.summary_floating': 'Bouton flottant', 'start.summary_spotlight': 'Spotlight',
    'start.summary_suggest': 'Suggestions quand un appel commence',
    'set.ui_language': 'Langue de l’application', 'set.ui_auto': 'Comme le Mac',
    'set.listen_on_launch': 'Commencer l’écoute à l’ouverture de Voicestream',
    'set.listen_on_launch_help': 'Sinon, cliquez vous-même sur Commencer l’écoute.',
    'set.start_at_login': 'Ouvrir Voicestream à l’ouverture de session', 'set.login_help': 'Voicestream démarre dans la barre des menus.',
    'set.login_unavailable': 'Disponible uniquement dans l’application Voicestream pour Mac.',
    'shortcut.label': 'Raccourci clavier', 'shortcut.choose': 'Choisir un raccourci', 'shortcut.turn_off': 'Désactiver',
    'shortcut.reset': 'Revenir à {keys}', 'shortcut.off': 'Désactivé', 'shortcut.key.space': 'Espace',
    'shortcut.press': 'Tapez le nouveau raccourci…', 'shortcut.press_help': 'Maintenez ⌘, ⌥ ou ⌃ et appuyez sur une touche. Échap pour annuler.',
    'shortcut.mac_only': 'Dans l’application Voicestream pour Mac, un raccourci clavier démarre et arrête l’écoute depuis n’importe quelle app.',
    'shortcut.mac_only_meeting': 'Dans l’application Voicestream pour Mac, un raccourci clavier démarre et arrête une réunion depuis n’importe quelle app.',
    'shortcut.status.ok': 'Fonctionne depuis n’importe quelle app, même fenêtre fermée : il démarre et arrête l’écoute.',
    'shortcut.status.ok_meeting': 'Fonctionne depuis n’importe quelle app, même fenêtre fermée : il démarre et arrête une réunion.',
    'shortcut.status.taken': 'Déjà utilisé par une autre app — choisissez-en un autre.',
    'shortcut.status.failed': 'macOS a refusé ce raccourci (erreur {code}) — choisissez-en un autre.',
    'shortcut.status.off': 'Désactivé. Démarrez et arrêtez l’écoute depuis la barre des menus ou cette fenêtre.',
    'shortcut.status.off_meeting': 'Désactivé. Démarrez et arrêtez les réunions depuis la barre des menus ou cette fenêtre.',
    'shortcut.why.unknown_key': 'Voicestream ne peut pas utiliser cette touche pour un raccourci : choisissez une lettre, un chiffre, une touche de fonction ou de ponctuation.',
    'shortcut.why.needs_modifier': 'Maintenez aussi ⌘, ⌥ ou ⌃ : seul, {keys} sert à écrire ou à autre chose.',
    'shortcut.why.one_modifier': '{keys} est déjà pris dans beaucoup d’apps (⌘C copie, ⌥E met un accent, ⌃C arrête une commande du Terminal) : maintenez une deuxième touche parmi ⌘, ⌥ ou ⌃, comme dans ⌥⌘R.',
    'shortcut.why.reserved_app': '{keys} est une commande de toutes les apps Mac (Quitter, Fermer, Masquer, Placer dans le Dock, Réglages) : choisissez-en un autre.',
    'shortcut.why.reserved_mac': 'macOS utilise lui-même {keys} (changer d’app ou de fenêtre, Spotlight, sources de saisie, captures d’écran, Forcer à quitter…) : choisissez-en un autre.',
    'shortcut.why.option_only': 'macOS 15 peut refuser un raccourci avec ⌥ seule, ou ⌥⇧ ({keys}) : maintenez aussi ⌘ ou ⌃.',
    'shortcut.sound': 'Jouer un son quand le raccourci démarre ou arrête l’écoute', 'shortcut.sound_help': 'Pour savoir que c’est fait sans regarder — et un autre son si l’écoute n’a pas pu démarrer.',
    'shortcut.sound_meeting': 'Jouer un son quand le raccourci démarre ou arrête une réunion', 'shortcut.sound_help_meeting': 'Pour savoir que c’est fait sans regarder, et un autre son si la réunion n’a pas pu démarrer.',
    'shortcut.hint': 'Vous pouvez aussi appuyer sur {keys} dans n’importe quelle app.',

    'lang.fr+en': 'Français et anglais', 'lang.fr+en.desc': 'L’une ou l’autre, même les deux dans une réunion.',
    'lang.fr': 'Français', 'lang.fr.desc': 'Réunions en français uniquement.',
    'lang.en': 'Anglais', 'lang.en.desc': 'Réunions en anglais uniquement.',
    'lang.any': 'Toutes les langues', 'lang.any.desc': 'Détecter la langue automatiquement.',
    'lang.other': 'Autre : {code}', 'lang.other.desc': 'Selon le fichier de réglages.',

    'stt.title': 'Transcription', 'stt.lead': 'Ce qui transforme les voix en texte.',
    'stt.summary': 'Utilisé :', 'stt.summary_model': 'Modèle : {name} · {size}', 'stt.summary_price': 'Environ {price} par heure de parole',
    'stt.local': 'Sur ce Mac — gratuit et privé', 'stt.local_desc': 'L’audio ne quitte jamais votre Mac. Un téléchargement unique.',
    'stt.openai': 'OpenAI — en ligne', 'stt.deepgram': 'Deepgram — en ligne',
    'stt.cloud_desc': 'Environ {price} par heure de parole. Demande une clé.',
    'stt.other': 'Autre : {name}', 'stt.other_desc': 'Selon le fichier de réglages.',
    'stt.models': 'Modèle de transcription', 'stt.models_help': 'Les grands modèles se trompent moins mais demandent plus de mémoire.',
    'stt.models_dir': 'Enregistré dans {dir}',
    'stt.vad_ok': 'Détecteur de voix : téléchargé.', 'stt.vad_downloading': 'Détecteur de voix : téléchargement…',
    'stt.vad_missing': 'Un petit détecteur de voix (1 Mo) se télécharge avec le modèle.',
    'stt.cancel': 'Annuler', 'stt.downloaded': 'Téléchargé', 'stt.remove': 'Supprimer', 'stt.remove_named': 'Supprimer {name}',
    'stt.download_size': 'Télécharger ({size})', 'stt.download_named': 'Télécharger {name}', 'stt.recommended': 'Recommandé',
    'stt.size_ram': '{size} · utilise environ {ram} Go de mémoire', 'stt.downloading': 'Progression du téléchargement',
    'stt.progress': '{pct} % · {done} sur {total}', 'stt.download_done': '« {name} » est prêt.', 'stt.download_failed': 'Échec du téléchargement de « {name} »',
    'stt.switch_after': '« {name} » sera utilisé dès qu’il sera téléchargé. D’ici là, Voicestream garde la transcription actuelle.',
    'stt.switched': 'La transcription utilise maintenant « {name} ».', 'stt.switched_later': '« {name} » est prêt : il sera utilisé à la fin de la réunion en cours.',

    'model.large-v3-turbo-q5_0.label': 'Idéal pour la plupart des Mac',
    'model.large-v3-turbo-q5_0.note': 'Presque la meilleure précision en français et en anglais. Convient à un Mac de 8 Go.',
    'model.large-v3-turbo.label': 'Meilleure qualité',
    'model.large-v3-turbo.note': 'Un peu plus précis. Pour les Mac de 16 Go de mémoire ou plus.',
    'model.small.label': 'Plus rapide', 'model.small.note': 'Plus rapide sur les Mac anciens, mais se trompe plus, surtout en français.',
    'model.base.label': 'Le plus léger', 'model.base.note': 'Très petit et rapide, nettement moins précis. Pour les très vieux Mac.',
    'model.silero-v6.2.0.label': 'Détection de la voix', 'model.silero-v6.2.0.note': 'Ignore les bruits pour ne rien inventer.',

    'key.saved_keychain': 'Enregistrée dans le trousseau', 'key.saved_file': 'Enregistrée sur cet ordinateur', 'key.not_set': 'Non renseignée',
    'key.kept_keychain': 'Gardée dans le trousseau macOS, jamais dans un fichier.', 'key.kept_file': 'Gardée dans un fichier privé sur cet ordinateur.',
    'key.paste': 'Collez-la ici…', 'key.paste_named': 'Collez votre {name}', 'key.save': 'Enregistrer la clé',
    'key.saved_toast': '{name} enregistrée.', 'key.replace': 'Remplacer', 'key.test': 'Tester', 'key.remove': 'Supprimer',
    'key.testing': 'Vérification…', 'key.remove_title': 'Supprimer la {name} ?',
    'key.remove_body': 'Voicestream cessera de l’utiliser jusqu’à ce que vous l’ajoutiez à nouveau.',
    'key.claude': 'clé Claude', 'key.claude_help': 'Commence par « sk-ant- ».',
    'key.where_claude': 'Où en obtenir une ?',
    'key.openai': 'clé OpenAI', 'key.openai_help': 'Commence par « sk- ».', 'key.where_openai': 'Obtenir une clé OpenAI',
    'key.deepgram': 'clé Deepgram', 'key.deepgram_help': 'Depuis votre console Deepgram.', 'key.where_deepgram': 'Obtenir une clé Deepgram',

    'perm.title': 'Autorisations', 'perm.lead': 'macOS demande une fois si Voicestream peut vous entendre, vous et la réunion.',
    'perm.mic': 'Micro', 'perm.mic_desc': 'Ce que vous dites.', 'perm.system': 'Son de la réunion',
    'perm.system_desc': 'Ce que disent les autres (Zoom, Teams, Meet…). Lancez une vidéo pendant le test.',
    'perm.test_mic': 'Tester le micro', 'perm.test_system': 'Tester le son de la réunion', 'perm.testing': 'Écoute…',
    'perm.mic_ok': 'Voicestream entend votre micro.', 'perm.system_ok': 'Voicestream entend le son de la réunion.',
    'perm.mic_denied': 'Voicestream n’entend pas le micro : autorisez-le dans les Réglages Système.',
    'perm.system_denied': 'Voicestream n’entend pas le son de la réunion : autorisez-le dans les Réglages Système.',
    'perm.mic_unknown': 'Impossible de savoir si le micro fonctionne.',
    'perm.system_unknown': 'Rien n’a été entendu. Lancez un son et testez à nouveau.',

    'claude.title': 'Claude', 'claude.lead': 'Claude (d’Anthropic) rédige les notes et repère les tâches. Il ne lit que du texte, jamais l’audio.',
    'claude.use': 'Utiliser Claude pour les notes et les tâches', 'claude.use_help': 'Désactivé : notes simples, rédigées hors ligne, gratuites.',
    'claude.model': 'Notes pendant les réunions', 'claude.model_haiku': 'Rapide et économique (recommandé)', 'claude.model_sonnet': 'Plus approfondi (plus cher)',
    'claude.model_help': 'Les notes rédigées après une réunion utilisent toujours le modèle le plus approfondi.',
    'claude.budget_title': 'Budget quotidien', 'claude.budget_lead': 'Une limite par jour pour Claude et la transcription en ligne.',
    'claude.budget': 'Dépenser au plus', 'claude.per_day': 'dollars US par jour',
    'claude.budget_help': 'Une fois atteint, les notes continuent hors ligne jusqu’au lendemain.', 'claude.see_costs': 'Voir vos dépenses',

    'audio.title': 'Audio', 'audio.lead': 'Ce que Voicestream écoute.', 'audio.mic': 'Micro',
    'audio.default_mic': 'Le micro par défaut du Mac', 'audio.default_suffix': '(par défaut)',
    'audio.mic_help': 'En général, celui par défaut convient ; choisissez votre casque si vous en utilisez un.',
    'audio.mic_fallback': 'Utiliser le micro par défaut du Mac tant que celui-ci n’est pas branché',
    'audio.mic_fallback_help': 'Désactivé : seul le son de la réunion est enregistré en attendant. Dans les deux cas, Voicestream revient dessus de lui-même.',
    'audio.no_devices': 'Aucun micro trouvé ({detail}).',
    'audio.system': 'Écouter aussi le son de la réunion', 'audio.system_help': 'Ce que disent les autres dans Zoom, Teams, Meet… Sans cela, seule votre voix est notée.',
    'audio.system_ok': 'Son de la réunion : {detail}', 'audio.system_missing': 'Le son de la réunion n’est pas disponible : {detail}',
    'audio.store': 'Garder un enregistrement de l’audio', 'audio.store_help': 'Enregistré avec chaque réunion. Désactivé par défaut : la transcription suffit en général.',

    'tools.lead': 'Où vont vos tâches et vos notes. Activez ceux que vous utilisez, puis cliquez sur Tester.',
    'tools.more': 'Autres outils', 'tools.more_help': 'Notion, Linear, Slack…', 'tools.enable': 'Activer {name}',
    'tools.summary': 'Activés :', 'tools.summary_none': 'Aucun n’est activé pour l’instant : les notes restent dans votre dossier Voicestream.',
    'tools.test': 'Tester', 'tools.testing': 'Test en cours…', 'tools.mac_only': 'Mac uniquement',
    'tools.save_first': 'Enregistrez d’abord vos modifications, puis testez.', 'tools.test_off': 'Activez-le et enregistrez d’abord.',
    'tools.test_ok': 'Fonctionne : {detail}',
    'tools.reminders.asking': 'Vérification de Rappels… Si macOS demande si Voicestream peut contrôler Rappels, cliquez sur OK.',
    'tools.only_mine': 'Seulement mes tâches', 'tools.only_mine_help': 'Désactivé : les tâches de tout le monde.',
    'tools.reminders.list': 'Liste de Rappels', 'tools.reminders.list_help': 'Créée si elle n’existe pas.',
    'tools.reminders.time': 'Heure du rappel', 'tools.reminders.time_help': 'Pour les tâches datées, ex. 09:00. Vide : sans heure.',
    'tools.markdown.todo': 'Fichier de la liste de tâches', 'tools.markdown.todo_ph': 'TODO.md dans votre dossier Voicestream',
    'tools.markdown.todo_help': 'Une liste en texte simple, lisible dans n’importe quelle app.',
    'tools.markdown.notes': 'Dossier des notes', 'tools.markdown.notes_ph': 'Le dossier de chaque réunion',
    'tools.markdown.notes_help': 'Copier aussi les notes de chaque réunion ici (par exemple un dossier synchronisé).',
    'tools.google.steps_title': 'Connecter Google (environ 5 minutes, une seule fois)',
    'tools.google.step1': 'Ouvrez', 'tools.google.step2': 'Effacez tout dans l’éditeur, collez le script passerelle et enregistrez.',
    'tools.google.step3': 'Paramètres du projet → Propriétés du script → ajoutez SECRET avec un nouveau secret.',
    'tools.google.step4': 'Déployer → Nouveau déploiement → Application Web. Exécuter en tant que : Moi. Accès : Tout le monde → Déployer.',
    'tools.google.step5': 'Autorisez l’accès avec votre compte Google (Paramètres avancés → Accéder à la passerelle).',
    'tools.google.step6': 'Copiez l’URL de l’application Web (elle finit par /exec) et collez-la ci-dessous.',
    'tools.google.copy_script': 'Copier le script passerelle', 'tools.google.copied': 'Le script passerelle est dans le presse-papiers.',
    'tools.google.make_secret': 'Créer un secret et le copier',
    'tools.google.secret_made': 'Un nouveau secret est enregistré et copié : collez-le comme propriété SECRET.',
    'tools.google.url': 'URL de l’application Web', 'tools.google.url_help': 'https://script.google.com/macros/s/…/exec',
    'tools.google.secret': 'Secret', 'tools.google.secret_help': 'Le même texte que la propriété de script SECRET.',
    'tools.google.calendar': 'Tâches dans Google Agenda', 'tools.google.calendar_help': 'Un rappel à la date d’échéance.',
    'tools.google.docs': 'Notes dans Google Docs', 'tools.google.docs_help': 'Un document par réunion.',
    'tools.google.recap': 'M’envoyer un récapitulatif', 'tools.google.recap_help': 'Envoyé à vous seul.',
    'tools.google.draft': 'E-mail de suivi en brouillon Gmail', 'tools.google.draft_help': 'Jamais envoyé tout seul.',
    'tools.hubspot.token': 'Jeton d’accès HubSpot', 'tools.hubspot.token_help': 'Depuis une application privée HubSpot (Paramètres → Intégrations → Applications privées).',
    'tools.hubspot.owner': 'Votre e-mail HubSpot', 'tools.hubspot.owner_help': 'Vos tâches deviennent des tâches HubSpot qui vous sont attribuées.',
    'tools.hubspot.log': 'Consigner les réunions sur les contacts', 'tools.hubspot.log_help': 'Les notes vont sur les contacts concernés.',
    'tools.shortcuts.lead': 'Lance un raccourci de l’app Raccourcis pour chaque tâche — par exemple pour l’ajouter à Promethee.',
    'tools.shortcuts.item': 'Raccourci pour chaque nouvelle tâche', 'tools.shortcuts.item_ph': 'ex. Ajouter à Promethee',
    'tools.shortcuts.notes': 'Raccourci pour les notes', 'tools.shortcuts.update': 'Raccourci quand une tâche change',
    'tools.command.lead': 'Lance une commande ou ouvre un lien d’app pour chaque tâche (avancé).',
    'tools.command.item': 'Commande pour chaque nouvelle tâche', 'tools.command.notes': 'Commande pour les notes',
    'tools.other_help': 'Ses options sont dans le fichier de réglages.',

    'tool.markdown.name': 'Fichiers sur ce Mac', 'tool.markdown.desc': 'Une liste de tâches et les notes, dans votre dossier Voicestream.',
    'tool.reminders.name': 'Rappels Apple', 'tool.reminders.desc': 'Vos tâches dans l’app Rappels, sur tous vos appareils.',
    'tool.google.name': 'Google', 'tool.google.desc': 'Rappels dans Agenda, notes dans Docs, brouillon Gmail de l’e-mail de suivi.',
    'tool.hubspot.name': 'HubSpot', 'tool.hubspot.desc': 'Tâches et notes de réunion dans votre CRM.',
    'tool.shortcuts.name': 'Raccourcis (Promethee…)', 'tool.shortcuts.desc': 'Envoyer chaque tâche à un raccourci.',
    'tool.command.name': 'Commande ou lien d’app', 'tool.command.desc': 'Pour les apps qui ont une ligne de commande ou un lien.',
    'tool.webhook.name': 'Webhook', 'tool.webhook.desc': 'Zapier, Make, n8n…',

    'agents.title': 'Agents', 'agents.lead': 'Un agent, c’est Claude qui fait une tâche pour vous (recherche, brouillon…) pendant que la réunion continue.',
    'agents.off': 'Désactivés', 'agents.off.desc': 'Ne jamais lancer d’agent.',
    'agents.suggest': 'Suggérer (recommandé)', 'agents.suggest.desc': 'Signaler les tâches qu’un agent pourrait faire ; il ne démarre que si vous cliquez sur « Confier à un agent ».',
    'agents.commands': 'Quand je le demande', 'agents.commands.desc': 'Démarrer aussi tout de suite quand vous dites « Hey Claude, … » en réunion.',
    'agents.auto': 'Automatique', 'agents.auto.desc': 'Lancer chaque tâche qu’un agent peut faire, dans la limite quotidienne.',
    'agents.daily': 'Les agents peuvent dépenser au plus', 'agents.daily_help': 'Une limite distincte du budget des notes.',
    'agents.per_task': 'Et au plus, par tâche', 'agents.per_task_help': 'Une tâche qui coûterait plus s’arrête.',
    'agents.per_task_suffix': 'dollars US',

    'adv.files': 'Fichiers', 'adv.files_lead': 'Où Voicestream garde ses réglages et vos réunions.',
    'adv.config': 'Fichier de réglages', 'adv.data': 'Dossier Voicestream', 'adv.version': 'Version',
    'adv.open_config': 'Afficher le fichier de réglages', 'adv.open_data': 'Afficher le dossier Voicestream', 'adv.open_log': 'Afficher le journal',
    'adv.health': 'Tout vérifier', 'adv.health_lead': 'Clés, micro, transcription et outils, en une fois.',
    'adv.check': 'Lancer la vérification', 'adv.checking': 'Vérification…',
    'adv.setup': 'Configuration', 'adv.setup_lead': 'Refaire la configuration du premier lancement.', 'adv.setup_again': 'Refaire la configuration',

    'setup.title': 'Configuration', 'setup.step': 'Étape {n} sur {total}', 'setup.close': 'Fermer la configuration',
    'setup.nav.welcome': 'Bienvenue', 'setup.nav.you': 'À propos de vous', 'setup.nav.stt': 'Transcription', 'setup.nav.claude': 'Claude',
    'setup.nav.perms': 'Autorisations', 'setup.nav.tools': 'Vos outils', 'setup.nav.start': 'Démarrer une réunion', 'setup.nav.done': 'Terminé',
    'setup.start.title': 'Comment démarrer une réunion',
    'setup.start.lead': 'Choisissez quand Voicestream enregistre. Vous pourrez le changer plus tard dans les Réglages.',
    'setup.start.manual_desc': 'Rien n’est enregistré tant que vous ne démarrez pas une réunion. Démarrez-la quand l’appel commence, arrêtez-la quand il se termine.',
    'setup.start.mac_only': 'Dans l’application Voicestream pour Mac, vous pouvez aussi démarrer une réunion avec un raccourci clavier, depuis un bouton flottant au-dessus de toutes les apps, depuis Spotlight, et quand un appel commence.',
    'setup.done.manual': 'Les réunions démarrent quand vous les lancez',
    'setup.done.manual_keys': 'Les réunions démarrent quand vous les lancez, ou avec {keys} dans n’importe quelle app',
    'setup.done.automatic': 'Les réunions démarrent d’elles-mêmes dès que quelqu’un parle',
    'setup.done.floating': 'Un bouton flottant au-dessus de toutes les apps démarre et arrête les réunions',
    'setup.done.spotlight': 'Spotlight démarre une réunion : « Voicestream – Nouvelle réunion » est dans votre dossier Applications',
    'setup.done.suggest': 'Suggestions quand un appel commence : Voicestream voit quelles apps utilisent le micro',
    'setup.done.finish': 'Terminer',
    'setup.downloading': 'Téléchargement du modèle de transcription', 'setup.back': 'Retour', 'setup.next': 'Continuer',
    'setup.welcome.title': 'Bienvenue dans Voicestream',
    'setup.welcome.lead': 'Démarrez une réunion quand vous le voulez : Voicestream en fait des notes claires et des listes de tâches, en français et en anglais.',
    'setup.welcome.p1': 'Privé par défaut', 'setup.welcome.p1_text': 'Les enregistrements et les transcriptions restent sur ce Mac. Rien n’est partagé, sauf vers les outils que vous connectez.',
    'setup.welcome.p2': 'Vous gardez la main', 'setup.welcome.p2_text': 'Une icône dans la barre des menus indique quand il écoute. Pause ou arrêt en un clic.',
    'setup.welcome.p3': 'Des notes rédigées pour vous', 'setup.welcome.p3_text': 'Claude lit la transcription (le texte seulement, jamais l’audio) pour rédiger les notes et repérer les tâches.',
    'setup.welcome.p4': 'Environ 3 minutes', 'setup.welcome.p4_text': 'Vous pourrez tout changer plus tard dans les Réglages.',
    'setup.welcome.start': 'Commencer',
    'setup.you.title': 'À propos de vous', 'setup.you.lead': 'Pour que vos tâches soient mises à votre nom.',
    'setup.you.languages': 'Langues parlées en réunion', 'setup.you.need_name': 'Indiquez votre nom.',
    'setup.stt.title': 'Transcription', 'setup.stt.lead': 'Ce qui transforme les voix en texte. Sur ce Mac, c’est gratuit et privé.',
    'setup.stt.local_desc': 'Gratuit, et l’audio ne quitte jamais votre Mac. Un téléchargement de {size}, qui commence maintenant.',
    'setup.stt.background': 'Le téléchargement continue en arrière-plan pendant que vous terminez la configuration.',
    'setup.stt.need_key': 'Enregistrez d’abord votre clé, ou choisissez « Sur ce Mac ».',
    'setup.file_problem': 'Votre fichier de réglages a un problème avec {what}, réglé en dehors de cette fenêtre : {detail}',
    'setup.file_problem_file': 'un réglage',
    'setup.claude.title': 'Connecter Claude', 'setup.claude.lead': 'Claude rédige les notes et repère les tâches. Collez votre clé Claude depuis la console Anthropic.',
    'setup.claude.cost': 'Une journée type de réunions coûte environ 0,10 à 0,50 $, dans un budget quotidien que vous fixez.',
    'setup.claude.privacy': 'Claude reçoit uniquement le texte de la transcription, jamais l’audio.',
    'setup.claude.need_key': 'Enregistrez d’abord votre clé — ou passez : les notes seront alors rédigées hors ligne (plus simples).',
    'setup.claude.skip': 'Passer — notes hors ligne',
    'setup.perms.title': 'Autorisations', 'setup.perms.lead': 'Vérifiez que Voicestream vous entend, vous et la réunion. macOS peut demander l’autorisation la première fois.',
    'setup.perms.help_mac': 'Si vous avez refusé, ouvrez Réglages Système → Confidentialité et sécurité et autorisez Voicestream, puis testez à nouveau.',
    'setup.perms.help_other': 'Sur cet ordinateur, les autorisations sont gérées par le système.',
    'setup.tools.title': 'Vos outils', 'setup.tools.lead': 'Où vont vos tâches après chaque réunion.',
    'setup.tools.list_default': 'Réunions',
    'setup.tools.reminders_help': 'Vos tâches apparaissent dans l’app Rappels, sur votre iPhone aussi.',
    'setup.tools.reminders_not_mac': 'Fonctionne uniquement sur Mac.',
    'setup.tools.files': 'Toujours actif : une liste de tâches et les notes sont enregistrées dans votre dossier Voicestream.',
    'setup.tools.later': 'Google, HubSpot, Raccourcis (Promethee) et d’autres : à configurer plus tard dans Réglages → Vos outils.',
    'setup.tools.check_again': 'Vérifier à nouveau',
    'setup.done.title': 'Tout est prêt, {name}', 'setup.done.lead': 'Voici comment Voicestream est configuré.',
    'setup.done.lang': 'Réunions en : {lang}', 'setup.done.stt_local': 'Transcription sur ce Mac — gratuite et privée',
    'setup.done.stt_downloading': 'Transcription sur ce Mac — le modèle est encore en téléchargement',
    'setup.done.stt_missing': 'Transcription sur ce Mac — téléchargez le modèle dans les Réglages',
    'setup.done.stt_cloud': 'Transcription avec {name}', 'setup.done.claude': 'Claude rédige vos notes',
    'setup.done.offline': 'Notes hors ligne (ajoutez une clé Claude plus tard dans les Réglages)',
    'setup.done.reminders': 'Les tâches vont dans Rappels Apple (liste « {list} »)',
    'setup.done.files': 'Les notes et les tâches sont enregistrées dans votre dossier Voicestream',
    'setup.done.start': 'Commencer l’écoute', 'setup.done.start_after': 'Commencer l’écoute à la fin du téléchargement',
    'setup.done.later': 'Plus tard', 'setup.done.will_start': 'Voicestream commencera l’écoute dès la fin du téléchargement.',
  },
};

// ============================================================================== boot

function has(key) { return (STRINGS[LANG] && STRINGS[LANG][key] != null) || STRINGS.en[key] != null; }

function applyLanguage(lang) {
  LANG = lang;
  document.documentElement.lang = lang;
  LV.root = null;
  MODEL_LISTS.clear();
  buildShell();
  buildLive();
  if (S.setup) renderSetup();
  else navigate(false);
}

function bootFailed(msg) {
  $('boot').hidden = false;
  $('boot').classList.add('failed');
  $('boot-text').textContent = msg;
}

async function boot() {
  LANG = detectLang(null);
  document.documentElement.lang = LANG;
  $('boot-text').textContent = t('boot.opening');
  if (!S.token) return bootFailed(t('conn.auth'));
  let st = null;
  try {
    st = BOOT.app ? await readJson(await BOOT.app) : await api('GET', '/api/app');
  } catch (err) {
    if (err.status === 401) return bootFailed(t('conn.auth'));
    try { st = await api('GET', '/api/app'); } catch (err2) {
      return bootFailed(err2.status === 401 ? t('conn.auth') : t('boot.failed', { detail: err2.message }));
    }
  }
  S.app = st;
  LANG = detectLang(st.prefs.ui_language);
  document.documentElement.lang = LANG;
  $('boot').hidden = true;
  buildShell();
  buildLive();
  if (!st.setup_done) openSetup();
  else {
    $('shell').hidden = false;
    navigate(false);
    // the window opens on Live with Start meeting in focus: Return starts one (not under a finger: a
    // focused button there only draws a ring)
    const start = liveFocusTarget();
    if (start && start.tagName === 'BUTTON' && !matchMedia('(pointer: coarse)').matches) focusLive(start);
  }
  connect();
  loadSettings().then(() => { if (S.route.name === 'live' && !S.setup) renderLive(); }).catch(() => { /* loaded again by Settings */ });
  setInterval(tick, 1000);
}

// for the UI tests: the markdown renderer on its own
window.voicestream = { renderMarkdown };

boot();
