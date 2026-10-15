// google_bridge.gs against in-memory fakes of the Apps Script services it uses.
// Run: node --test tests/js/google_bridge.test.mjs (tests/test_sink_google.py runs it too).

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('../../src/voicestream/google_bridge.gs', import.meta.url), 'utf8');
const SECRET = 'correct-horse-battery-staple';
const ME = 'alex@gmail.com';
const plain = (x) => JSON.parse(JSON.stringify(x)); // objects made inside the vm have foreign prototypes
const isDate = (d) => Object.prototype.toString.call(d) === '[object Date]' && !Number.isNaN(d.getTime());

class FakeEvent {
  constructor(cal, id, title, start, end, opts) {
    Object.assign(this, {cal, id, title, start, end, description: opts.description, tags: {}, popups: [30], transparency: 'OPAQUE'});
  }
  getId() { return this.id; }
  setTitle(t) { this.title = t; return this; }
  setDescription(d) { this.description = d; return this; }
  setTime(s, e) { assert.ok(isDate(s) && isDate(e)); this.start = s; this.end = e; return this; }
  setTransparency(t) { assert.ok(t); this.transparency = t; return this; }
  removeAllReminders() { this.popups = []; return this; }
  addPopupReminder(m) {
    if (!(m >= 5 && m <= 40320)) throw new Error('Invalid argument: minutesBefore');
    this.popups.push(m);
    return this;
  }
  setTag(k, v) { this.tags[k] = String(v); return this; }
  getTag(k) { return k in this.tags ? this.tags[k] : null; }
  deleteEvent() { this.cal.events.delete(this.id); }
}

class FakeCalendar {
  constructor(g, id) { Object.assign(this, {g, id, events: new Map()}); }
  getId() { return this.id; }
  createEvent(title, start, end, opts) {
    if (this.g.fail.createEvent) throw new Error(this.g.fail.createEvent);
    if (!isDate(start) || !isDate(end)) throw new Error('Invalid argument: startTime');
    const ev = new FakeEvent(this, `ev${++this.g.seq}@google.com`, title, start, end, opts || {});
    this.events.set(ev.id, ev);
    return ev;
  }
  getEventById(id) { return this.events.get(id) || null; }
}

class FakeElement {
  constructor(body, type, text) {
    // Docs turns "\n" inside a paragraph into a soft line break ("\r")
    Object.assign(this, {body, type, text: String(text).replace(/\n/g, '\r'), styles: [], attrs: {}});
  }
  editAsText() {
    const el = this;
    const ranged = (name) => function (...args) {
      if (args.length === 1) {
        el.styles.push([name, 'all', args[0]]);
      } else {
        const [start, end, value] = args;
        if (!(start >= 0 && end >= start && end < el.text.length)) throw new Error(`Invalid offsets ${start}..${end}`);
        el.styles.push([name, el.text.slice(start, end + 1), value]);
      }
      return this;
    };
    return {setBold: ranged('bold'), setItalic: ranged('italic'), setStrikethrough: ranged('strike'),
      setFontFamily: ranged('font'), setLinkUrl: ranged('link')};
  }
  setHeading(h) { assert.ok(h, 'unknown heading'); this.attrs.heading = h; return this; }
  setIndentStart(v) { this.attrs.indentStart = v; return this; }
  setIndentFirstLine(v) { this.attrs.indentFirstLine = v; return this; }
  setNestingLevel(n) { this.attrs.level = n; return this; }
  setGlyphType(g) { assert.ok(g, 'unknown glyph'); this.attrs.glyph = g; return this; }
  setListId(item) { this.attrs.listId = item.listId(); return this; }
  listId() { return this.attrs.listId || this.ownList; }
  removeFromParent() { this.body.children.splice(this.body.children.indexOf(this), 1); return this; }
}

class FakeBody {
  constructor() { this.lists = 0; this.clear(); }
  clear() { this.children = [new FakeElement(this, 'PARAGRAPH', '')]; return this; }
  append(type, text) { const el = new FakeElement(this, type, text); this.children.push(el); return el; }
  appendParagraph(t) { return this.append('PARAGRAPH', t); }
  appendListItem(t) { const el = this.append('LIST_ITEM', t); el.ownList = `list${++this.lists}`; return el; }
  appendHorizontalRule() { return this.append('HORIZONTAL_RULE', ''); }
  getNumChildren() { return this.children.length; }
  getChild(i) { return this.children[i]; }
}

function folderOf(g, name, owner, trashed = false) {
  const id = `folder${++g.seq}`;
  return {id, name, getId: () => id, getName: () => name, isTrashed: () => trashed,
    getOwner: () => (owner ? {getEmail: () => owner} : null)};
}

function fakeGoogle({secret = SECRET} = {}) {
  const g = {props: {SECRET: secret}, cache: new Map(), seq: 0, docs: new Map(), files: new Map(), folders: [],
    sent: [], drafts: [], locked: 0, fail: {}, outputs: []};
  g.primary = new FakeCalendar(g, ME);
  g.team = new FakeCalendar(g, 'team@group.calendar.google.com');
  const globals = {
    PropertiesService: {getScriptProperties: () => ({getProperty: (k) => (k in g.props ? g.props[k] : null)})},
    LockService: {getScriptLock: () => ({
      waitLock() {
        if (g.fail.lock) throw new Error('Lock timeout: another process was holding the lock for too long.');
        g.locked++;
      },
      releaseLock() { g.locked--; },
    })},
    CacheService: {getScriptCache: () => ({
      get: (k) => (g.cache.has(k) ? g.cache.get(k) : null),
      put: (k, v, ttl) => {
        assert.ok(k.length <= 250 && ttl >= 1 && ttl <= 21600 && v.length < 100_000);
        g.cache.set(k, v);
      },
    })},
    ContentService: {
      MimeType: {JSON: 'JSON', TEXT: 'TEXT'},
      createTextOutput: (content) => {
        const out = {content, mime: 'TEXT', setMimeType(m) { this.mime = m; return this; }};
        g.outputs.push(out);
        return out;
      },
    },
    Session: {getEffectiveUser: () => ({getEmail: () => ME})},
    CalendarApp: {
      getDefaultCalendar: () => g.primary,
      getCalendarById: (id) => [g.primary, g.team].find((c) => c.id === id) || null,
      EventTransparency: {OPAQUE: 'OPAQUE', TRANSPARENT: 'TRANSPARENT'},
    },
    DocumentApp: {
      ParagraphHeading: Object.fromEntries(['NORMAL', 'HEADING1', 'HEADING2', 'HEADING3', 'HEADING4', 'HEADING5', 'HEADING6', 'TITLE', 'SUBTITLE'].map((k) => [k, k])),
      GlyphType: Object.fromEntries(['BULLET', 'HOLLOW_BULLET', 'SQUARE_BULLET', 'NUMBER', 'LATIN_UPPER', 'LATIN_LOWER', 'ROMAN_UPPER', 'ROMAN_LOWER'].map((k) => [k, k])),
      create: (name) => {
        const id = `doc${++g.seq}`;
        const doc = {id, name, body: new FakeBody(), saved: 0,
          getId: () => id, getUrl: () => `https://docs.google.com/document/d/${id}/edit`, getBody: () => doc.body,
          setName(n) { doc.name = n; return doc; }, saveAndClose() { doc.saved++; }};
        g.docs.set(id, doc);
        g.files.set(id, {trashed: false, parent: 'My Drive', description: null});
        return doc;
      },
      openById: (id) => {
        if (!g.docs.has(id)) throw new Error("Document is missing (perhaps it was deleted, or you don't have read access?)");
        return g.docs.get(id);
      },
    },
    DriveApp: {
      getFileById: (id) => {
        const f = g.files.get(id);
        if (!f) throw new Error('No item with the given ID could be found. Possibly because you have not edited this item or you do not have permission to access it.');
        return {isTrashed: () => f.trashed, getDescription: () => f.description,
          setDescription(d) { f.description = d; return this; },
          moveTo(folder) { f.parent = folder.getName() + '#' + folder.getId(); return this; }};
      },
      getFoldersByName: (name) => {
        const found = g.folders.filter((f) => f.name === name);
        let i = 0;
        return {hasNext: () => i < found.length, next: () => found[i++]};
      },
      createFolder: (name) => {
        const f = folderOf(g, name, ME);
        g.folders.push(f);
        return f;
      },
    },
    GmailApp: {
      sendEmail: (to, subject, body, opts) => {
        if (g.fail.send) throw new Error(g.fail.send);
        g.sent.push(plain({to, subject, body, opts}));
      },
      createDraft: (to, subject, body, opts) => {
        g.drafts.push(plain({to, subject, body, opts}));
        return {getId: () => `r-${g.drafts.length}`};
      },
    },
  };
  const ctx = vm.createContext(globals);
  vm.runInContext(SOURCE, ctx, {filename: 'google_bridge.gs'});
  g.ctx = ctx;
  g.post = (contents) => JSON.parse(ctx.doPost({postData: {contents, type: 'application/json'}}).content);
  g.call = (action, fields = {}) => g.post(JSON.stringify({secret: SECRET, action, ...fields}));
  return g;
}

const TASK = {calendar_id: 'primary', title: 'Envoyer le devis à Léa 🧾', description: 'Responsable : Alex',
  start: '2026-10-16T09:00:00+02:00', end: '2026-10-16T09:30:00+02:00', day: '2026-10-16', popup_minutes: 10};

// ----------------------------------------------------------------------- auth

test('ping answers JSON with the account and version', () => {
  const g = fakeGoogle();
  assert.deepEqual(g.call('ping'), {email: ME, version: 1, ok: true});
  assert.equal(g.outputs.at(-1).mime, 'JSON');
});

test('requests without the right secret are refused before doing anything', () => {
  const g = fakeGoogle();
  for (const secret of ['wrong-horse-battery-staple', SECRET.slice(0, -1), undefined, 123, null, ['x']]) {
    const out = g.post(JSON.stringify({secret, action: 'task.create', ...TASK}));
    assert.equal(out.ok, false);
    assert.equal(out.retryable, false);
    assert.match(out.error, /wrong secret/);
  }
  assert.equal(g.primary.events.size, 0);
});

test('an unset or short SECRET property gives setup instructions', () => {
  for (const secret of [undefined, 'short']) {
    const g = fakeGoogle({secret});
    if (secret === undefined) delete g.props.SECRET;
    const out = g.call('ping');
    assert.equal(out.ok, false);
    assert.equal(out.retryable, false);
    assert.match(out.error, /Project Settings → Script Properties → Add script property, name SECRET/);
  }
});

test('bad bodies and unknown actions are errors, never echoes', () => {
  const g = fakeGoogle();
  assert.equal(g.post('not json').ok, false);
  assert.equal(g.post('null').ok, false);
  assert.equal(JSON.parse(g.ctx.doPost(undefined).content).ok, false); // run from the editor
  for (const action of ['nope', '__proto__', 'constructor', 'hasOwnProperty', 'toString', 'handle', 'doPost']) {
    const out = g.call(action);
    assert.equal(out.ok, false, action);
    assert.match(out.error, /unknown action/);
    assert.ok(!JSON.stringify(out).includes(SECRET));
  }
});

// ------------------------------------------------------------------- calendar

test('task.create adds a free, single-popup event tagged with its due date', () => {
  const g = fakeGoogle();
  const out = g.call('task.create', TASK);
  assert.deepEqual(out, {calendar_id: ME, event_id: 'ev1@google.com', ok: true});
  const ev = g.primary.events.get('ev1@google.com');
  assert.equal(ev.title, 'Envoyer le devis à Léa 🧾');
  assert.equal(ev.description, 'Responsable : Alex');
  assert.equal(ev.start.toISOString(), '2026-10-16T07:00:00.000Z');
  assert.equal(ev.end.toISOString(), '2026-10-16T07:30:00.000Z');
  assert.equal(ev.transparency, 'TRANSPARENT');
  assert.deepEqual(ev.popups, [10]);
  assert.deepEqual(ev.tags, {voicestream_day: '2026-10-16'});
});

test('a retried create (same key) updates the first event instead of adding one', () => {
  const g = fakeGoogle();
  const first = g.call('task.create', {...TASK, key: 'vs:k1'});
  const again = g.call('task.create', {...TASK, key: 'vs:k1', title: 'Envoyer le devis signé'});
  assert.equal(again.event_id, first.event_id);
  assert.equal(g.primary.events.size, 1);
  assert.equal(g.primary.events.get(first.event_id).title, 'Envoyer le devis signé');
  assert.equal(g.call('task.create', {...TASK, key: 'vs:k2'}).event_id, 'ev2@google.com');
  assert.equal(g.locked, 0);
});

test('task.create can target another calendar; an unknown one is a config error', () => {
  const g = fakeGoogle();
  assert.equal(g.call('task.create', {...TASK, calendar_id: 'team@group.calendar.google.com'}).calendar_id, 'team@group.calendar.google.com');
  assert.equal(g.team.events.size, 1);
  const out = g.call('task.create', {...TASK, calendar_id: 'nope@group.calendar.google.com'});
  assert.equal(out.ok, false);
  assert.equal(out.retryable, false);
  assert.match(out.error, /no calendar "nope@group.calendar.google.com"/);
});

test('task.update renames, moves only on a new due date, deletes, and tolerates a missing event', () => {
  const g = fakeGoogle();
  const {event_id} = g.call('task.create', TASK);
  const ev = g.primary.events.get(event_id);
  ev.setTime(new Date('2026-10-16T13:00:00Z'), new Date('2026-10-16T13:30:00Z')); // you moved it in Calendar

  assert.deepEqual(g.call('task.update', {...TASK, event_id, title: '✓ Envoyer le devis', description: 'fait'}), {ok: true});
  assert.equal(ev.title, '✓ Envoyer le devis');
  assert.equal(ev.description, 'fait');
  assert.equal(ev.start.toISOString(), '2026-10-16T13:00:00.000Z'); // same due date: your time is kept

  g.call('task.update', {...TASK, event_id, day: '2026-10-20', start: '2026-10-20T09:00:00+02:00', end: '2026-10-20T09:30:00+02:00'});
  assert.equal(ev.start.toISOString(), '2026-10-20T07:00:00.000Z');
  assert.equal(ev.tags.voicestream_day, '2026-10-20');

  assert.deepEqual(g.call('task.update', {calendar_id: ME, event_id, remove: true}), {deleted: true, ok: true});
  assert.equal(g.primary.events.size, 0);
  assert.deepEqual(g.call('task.update', {...TASK, calendar_id: ME, event_id}), {gone: true, ok: true});
});

test('task.update leaves events it did not make alone', () => {
  const g = fakeGoogle();
  const yours = g.primary.createEvent('Dentist', new Date('2026-10-16T08:00:00Z'), new Date('2026-10-16T09:00:00Z'), {});
  for (const req of [{...TASK, event_id: yours.id}, {calendar_id: ME, event_id: yours.id, remove: true}]) {
    assert.deepEqual(g.call('task.update', req), {gone: true, ok: true});
  }
  assert.equal(g.primary.events.get(yours.id).title, 'Dentist');
});

test('quota and lock errors say whether a retry can help', () => {
  const g = fakeGoogle();
  g.fail.createEvent = 'Service invoked too many times in a short time: calendar. Try Utilities.sleep(1000) between calls.';
  assert.deepEqual(g.call('task.create', {...TASK, key: 'vs:a'}), {ok: false, error: g.fail.createEvent, retryable: true});
  g.fail.createEvent = 'Service invoked too many times for one day: calendar.';
  assert.equal(g.call('task.create', {...TASK, key: 'vs:a'}).retryable, false);
  assert.equal(g.locked, 0, 'the lock is released when the action throws');
  delete g.fail.createEvent;
  g.fail.lock = true;
  assert.equal(g.call('task.create', {...TASK, key: 'vs:a'}).retryable, true);
  assert.equal(g.primary.events.size, 0);
});

// ---------------------------------------------------------------------- notes

const NOTES = {
  key: 'vs:notes1',
  doc: {id: null, title: '2026-10-14 Point hebdo', folder: 'Meeting notes', markdown: '# Point hebdo\n\n- [ ] **Envoyer le devis** — Alex'},
  recap: {subject: 'Notes : Point hebdo', html: '<h1>Point hebdo</h1>', text: '# Point hebdo', link_label: 'Ouvrir <dans> "Docs"'},
  draft: {to: ['sam@example.com', 'lea@example.fr'], subject: 'Suivi', html: '<p>Bonjour</p>', text: 'Bonjour'},
};

test('meeting.notes writes the Doc in your folder, e-mails only you, and leaves a draft', () => {
  const g = fakeGoogle();
  g.folders.push(folderOf(g, 'Meeting notes', 'someone@else.com'), folderOf(g, 'Meeting notes', ME, true));
  const out = g.call('meeting.notes', NOTES);
  assert.deepEqual(out, {ok: true, warnings: [], doc_id: 'doc3', doc_url: 'https://docs.google.com/document/d/doc3/edit', recap_sent: true, draft_id: 'r-1'});

  const doc = g.docs.get('doc3');
  assert.equal(doc.name, '2026-10-14 Point hebdo');
  assert.equal(g.files.get('doc3').description, 'Meeting notes written by voicestream');
  assert.equal(doc.saved, 1);
  const mine = g.folders.find((f) => f.getOwner()?.getEmail() === ME && !f.isTrashed());
  assert.equal(g.files.get('doc3').parent, `Meeting notes#${mine.id}`); // not the shared or the trashed one
  assert.equal(g.folders.length, 3);

  assert.equal(g.sent.length, 1);
  const mail = g.sent[0];
  assert.equal(mail.to, ME);
  assert.equal(mail.subject, 'Notes : Point hebdo');
  assert.equal(mail.opts.name, 'voicestream');
  assert.equal(mail.opts.htmlBody,
    '<p><a href="https://docs.google.com/document/d/doc3/edit">Ouvrir &lt;dans&gt; &quot;Docs&quot;</a></p>\n<h1>Point hebdo</h1>');
  assert.equal(mail.body, '# Point hebdo\n\nOuvrir <dans> "Docs": https://docs.google.com/document/d/doc3/edit\n');

  assert.deepEqual(g.drafts, [{to: 'sam@example.com,lea@example.fr', subject: 'Suivi', body: 'Bonjour', opts: {htmlBody: '<p>Bonjour</p>'}}]);
  assert.equal(g.locked, 0);
});

test('an existing folder of yours is reused', () => {
  const g = fakeGoogle();
  const mine = folderOf(g, 'Meeting notes', ME);
  g.folders.push(mine);
  const {doc_id} = g.call('meeting.notes', {key: 'vs:n', doc: NOTES.doc});
  assert.equal(g.files.get(doc_id).parent, `Meeting notes#${mine.id}`);
  assert.equal(g.folders.length, 1);
});

test('a retried meeting.notes rewrites the same Doc and sends nothing twice', () => {
  const g = fakeGoogle();
  const first = g.call('meeting.notes', NOTES);
  const again = g.call('meeting.notes', {...NOTES, doc: {...NOTES.doc, markdown: 'v2'}});
  assert.equal(again.doc_id, first.doc_id);
  assert.equal(again.draft_id, 'r-1');
  assert.equal(again.recap_sent, true);
  assert.equal(g.docs.size, 1);
  assert.equal(g.sent.length, 1);
  assert.equal(g.drafts.length, 1);
  assert.deepEqual(g.docs.get(first.doc_id).body.children.map((c) => c.text), ['v2']);
});

test('pushing notes again with the Doc id renames and rewrites it; a deleted or trashed Doc is recreated', () => {
  const g = fakeGoogle();
  const {doc_id} = g.call('meeting.notes', {key: 'vs:a', doc: NOTES.doc});
  const out = g.call('meeting.notes', {key: 'vs:b', doc: {...NOTES.doc, id: doc_id, title: '2026-10-14 Budget 2027', markdown: 'nouveau'}});
  assert.equal(out.doc_id, doc_id);
  assert.equal(g.docs.get(doc_id).name, '2026-10-14 Budget 2027');
  assert.deepEqual(g.docs.get(doc_id).body.children.map((c) => c.text), ['nouveau']);

  g.files.get(doc_id).trashed = true;
  const trashed = g.call('meeting.notes', {key: 'vs:c', doc: {...NOTES.doc, id: doc_id}});
  assert.notEqual(trashed.doc_id, doc_id);
  const gone = g.call('meeting.notes', {key: 'vs:d', doc: {...NOTES.doc, id: 'doc-deleted'}});
  assert.equal(gone.ok, true);
  assert.equal(g.docs.size, 3);
});

test('meeting.notes never overwrites a Doc it did not make', () => {
  const g = fakeGoogle();
  const thesis = g.ctx.DocumentApp.create('Thesis');
  thesis.body.appendParagraph('years of work');
  const out = g.call('meeting.notes', {key: 'vs:t', doc: {...NOTES.doc, id: thesis.getId()}});
  assert.notEqual(out.doc_id, thesis.getId());
  assert.equal(thesis.name, 'Thesis');
  assert.deepEqual(thesis.body.children.map((c) => c.text), ['', 'years of work']);
});

test('a failed recap is a warning: the Doc and the draft still happen', () => {
  const g = fakeGoogle();
  g.fail.send = 'Service invoked too many times for one day: email.';
  const out = g.call('meeting.notes', NOTES);
  assert.equal(out.ok, true);
  assert.equal(out.recap_sent, false);
  assert.equal(out.draft_id, 'r-1');
  assert.ok(out.doc_url);
  assert.deepEqual(out.warnings, ['recap e-mail: Service invoked too many times for one day: email.']);
});

test('without a doc, the recap has no link', () => {
  const g = fakeGoogle();
  const out = g.call('meeting.notes', {key: 'vs:x', doc: null, recap: NOTES.recap, draft: null});
  assert.equal(out.doc_url, undefined);
  assert.equal(g.sent[0].opts.htmlBody, '<h1>Point hebdo</h1>');
  assert.equal(g.sent[0].body, '# Point hebdo');
});

// ---------------------------------------------------- Markdown → Google Docs

const MD = `# Réunion **budget** 2027

*mercredi 14 octobre 2026, 10:00* · 23:05 enregistrées

> Le devis part vendredi.
> Deuxième ligne

## Actions

- [ ] **Envoyer le devis** — Alex · échéance 2026-10-16
  Avec la remise de 5 %
- [x] Réserver la salle
- Point sur \`config.toml\` et [le doc](https://docs.example.com/a?b=1&c=2)
  - sous-point [piège](javascript:void) [x](data:text/html,hi) et [mail](mailto:lea@example.fr) [p](javascript:alert(1))
1. un
2. deux

Bonjour à tous,
Cordialement, ~~Alexis~~ Alex

\`\`\`
**pas du gras** <b>
\`\`\`

---
snake_case_name et _italique_ 🤖 **après emoji**`;

function rendered(md) {
  const g = fakeGoogle();
  const {doc_id} = g.call('meeting.notes', {key: 'vs:md', doc: {id: null, title: 't', folder: 'f', markdown: md}});
  return g.docs.get(doc_id).body.children;
}

test('the Markdown renderer builds headings, lists, checklists, quotes, code and rules', () => {
  const els = rendered(MD).map((el) => ({type: el.type, text: el.text, ...el.attrs, ...(el.styles.length ? {styles: el.styles} : {})}));
  assert.deepEqual(els, [
    {type: 'PARAGRAPH', text: 'Réunion budget 2027', heading: 'HEADING1', styles: [['bold', 'budget', true]]},
    {type: 'PARAGRAPH', text: 'mercredi 14 octobre 2026, 10:00 · 23:05 enregistrées', heading: 'NORMAL',
      styles: [['italic', 'mercredi 14 octobre 2026, 10:00', true]]},
    {type: 'PARAGRAPH', text: 'Le devis part vendredi.\rDeuxième ligne', heading: 'NORMAL', indentStart: 36, indentFirstLine: 36,
      styles: [['italic', 'all', true]]},
    {type: 'PARAGRAPH', text: 'Actions', heading: 'HEADING2'},
    {type: 'PARAGRAPH', text: '☐ Envoyer le devis — Alex · échéance 2026-10-16\rAvec la remise de 5 %', heading: 'NORMAL',
      indentFirstLine: 0, indentStart: 18, styles: [['bold', 'Envoyer le devis', true]]},
    {type: 'PARAGRAPH', text: '☑ Réserver la salle', heading: 'NORMAL', indentFirstLine: 0, indentStart: 18},
    {type: 'LIST_ITEM', text: 'Point sur config.toml et le doc', level: 0, indentStart: 36, indentFirstLine: 18, glyph: 'BULLET', heading: 'NORMAL',
      styles: [['font', 'config.toml', 'Courier New'], ['link', 'le doc', 'https://docs.example.com/a?b=1&c=2']]},
    {type: 'LIST_ITEM', text: 'sous-point piège x et mail [p](javascript:alert(1))', level: 1, indentStart: 72, indentFirstLine: 54, glyph: 'BULLET',
      listId: 'list1', heading: 'NORMAL', styles: [['link', 'mail', 'mailto:lea@example.fr']]},
    {type: 'LIST_ITEM', text: 'un', level: 0, indentStart: 36, indentFirstLine: 18, glyph: 'NUMBER', heading: 'NORMAL'},
    {type: 'LIST_ITEM', text: 'deux', level: 0, indentStart: 36, indentFirstLine: 18, glyph: 'NUMBER', listId: 'list3', heading: 'NORMAL'},
    {type: 'PARAGRAPH', text: 'Bonjour à tous,\rCordialement, Alexis Alex', heading: 'NORMAL', styles: [['strike', 'Alexis', true]]},
    {type: 'PARAGRAPH', text: '**pas du gras** <b>', heading: 'NORMAL', styles: [['font', 'all', 'Courier New']]},
    {type: 'HORIZONTAL_RULE', text: ''},
    {type: 'PARAGRAPH', text: 'snake_case_name et italique 🤖 après emoji', heading: 'NORMAL',
      styles: [['italic', 'italique', true], ['bold', 'après emoji', true]]},
  ]);
});

test('inline offsets are UTF-16 indexes into the final text, whatever the emoji and accents', () => {
  const g = fakeGoogle();
  for (const md of ['🤖🤖 **é** *à* `ç` [ü](https://x.fr) ~~ß~~', '**a** **b**', '*a* *b*', 'x*no*x snake_case_word __gras__', '_a_b_']) {
    const {text, marks} = g.ctx.parseInline(md);
    for (const m of marks) assert.ok(m.end > m.start && text.slice(m.start, m.end).length > 0, md);
  }
  const {text, marks} = g.ctx.parseInline('🤖🤖 **é** *à* `ç` [ü](https://x.fr) ~~ß~~');
  assert.equal(text, '🤖🤖 é à ç ü ß');
  assert.deepEqual(plain(marks).map((m) => [m.style, text.slice(m.start, m.end)]),
    [['bold', 'é'], ['italic', 'à'], ['code', 'ç'], ['link', 'ü'], ['strike', 'ß']]);
  assert.deepEqual(plain(g.ctx.parseInline('x*no*x snake_case_word __gras__').marks).map((m) => m.style), ['bold']);
  assert.equal(g.ctx.parseInline('_a_b_').text, 'a_b'); // intraword _ doesn't close, as in CommonMark
});

test('an empty Markdown body leaves one empty paragraph', () => {
  assert.deepEqual(rendered('').map((el) => el.text), ['']);
});
