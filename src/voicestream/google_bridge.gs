/**
 * voicestream Google bridge: lets voicestream put reminders in your Google Calendar,
 * write meeting notes to Google Docs, e-mail you a recap and leave follow-up drafts in
 * Gmail. It runs as you, so there is no Google Cloud project, OAuth client or token to
 * maintain. Setup steps: see the docstring of voicestream/sinks/google.py.
 *
 * Protocol: voicestream POSTs JSON {secret, action, ...} and gets back {ok: true, ...}
 * or {ok: false, error, retryable}. Web apps can't read request headers, so the secret
 * travels in the body. A request with a "key" is idempotent for 6 hours: if an answer
 * gets lost and voicestream retries, the bridge finishes and returns what the first
 * attempt created instead of creating it again. It only edits events and Docs it made
 * itself, e-mails only you, and never sends a draft.
 */

var VERSION = 1;
var MIN_SECRET = 16;
var KEY_TTL_S = 21600; // the CacheService maximum
var LOCK_WAIT_MS = 30000;
var DAY_TAG = 'voicestream_day'; // also marks the events this bridge made
var DOC_MARK = 'Meeting notes written by voicestream'; // the Drive description of its Docs
var INDENT_PT = 36;
var TRANSIENT = /timed? ?out|try again|too many times in a short time|temporar|unavailable|server error|backend error|\block\b/i;
var SAFE_URL = /^(?:https?:\/\/|mailto:)[^\s<>"'`]+$/i;
var BOXES = {' ': '☐', x: '☑', X: '☑'};

var ACTIONS = {
  'ping': ping,
  'task.create': createTask,
  'task.update': updateTask,
  'meeting.notes': meetingNotes,
};

class BridgeError extends Error {}

function doPost(e) {
  var out;
  try {
    out = handle(JSON.parse(e.postData.contents));
    out.ok = true;
  } catch (err) {
    var message = String((err && err.message) || err);
    out = {ok: false, error: message, retryable: !(err instanceof BridgeError) && TRANSIENT.test(message)};
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

function handle(req) {
  var secret = PropertiesService.getScriptProperties().getProperty('SECRET');
  if (!secret || secret.length < MIN_SECRET) {
    throw new BridgeError(
      'the bridge has no SECRET yet: in the Apps Script editor open Project Settings → Script Properties → ' +
        'Add script property, name SECRET, value a random string of ' + MIN_SECRET + '+ characters (the same as ' +
        'VOICESTREAM_GOOGLE_SECRET), then Save script properties'
    );
  }
  if (!sameSecret(req.secret, secret)) {
    throw new BridgeError('wrong secret: VOICESTREAM_GOOGLE_SECRET must equal the SECRET script property');
  }
  if (!Object.prototype.hasOwnProperty.call(ACTIONS, req.action)) {
    throw new BridgeError('unknown action "' + req.action + '" (this is bridge v' + VERSION + '; update google_bridge.gs?)');
  }
  var action = ACTIONS[req.action];
  if (!req.key) return action(req, {}, function () {});
  // one request at a time, so a retry waits for the attempt it repeats and then sees its state
  var lock = LockService.getScriptLock();
  lock.waitLock(LOCK_WAIT_MS);
  try {
    var cache = CacheService.getScriptCache();
    var state = JSON.parse(cache.get(req.key) || '{}');
    return action(req, state, function () {
      cache.put(req.key, JSON.stringify(state), KEY_TTL_S);
    });
  } finally {
    lock.releaseLock();
  }
}

function sameSecret(given, expected) {
  if (typeof given !== 'string' || given.length !== expected.length) return false;
  var diff = 0;
  for (var i = 0; i < expected.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

function ping() {
  return {email: Session.getEffectiveUser().getEmail(), version: VERSION};
}

// ------------------------------------------------------------------- calendar

function calendar(id) {
  var cal = !id || id === 'primary' ? CalendarApp.getDefaultCalendar() : CalendarApp.getCalendarById(id);
  if (!cal) throw new BridgeError('no calendar "' + id + '" in this Google account (check calendar_id)');
  return cal;
}

function createTask(req, state, save) {
  var cal = calendar(req.calendar_id);
  var ev = state.event_id ? cal.getEventById(state.event_id) : null;
  if (ev) {
    applyTask(ev, req); // a retry of a create that went through
  } else {
    ev = cal.createEvent(req.title, new Date(req.start), new Date(req.end), {description: req.description});
    state.event_id = ev.getId();
    save();
    ev.setTag(DAY_TAG, req.day)
      .setTransparency(CalendarApp.EventTransparency.TRANSPARENT)
      .removeAllReminders()
      .addPopupReminder(req.popup_minutes);
  }
  return {calendar_id: cal.getId(), event_id: ev.getId()};
}

function updateTask(req) {
  var ev = calendar(req.calendar_id).getEventById(req.event_id);
  if (!ev || !ev.getTag(DAY_TAG)) return {gone: true}; // deleted, or not one of ours
  if (req.remove) {
    ev.deleteEvent();
    return {deleted: true};
  }
  applyTask(ev, req);
  return {};
}

function applyTask(ev, req) {
  ev.setTitle(req.title).setDescription(req.description);
  // move it only when the due date changed, so a time you picked in Calendar sticks
  if (ev.getTag(DAY_TAG) !== req.day) ev.setTime(new Date(req.start), new Date(req.end)).setTag(DAY_TAG, req.day);
}

// ---------------------------------------------------------------------- notes

function meetingNotes(req, state, save) {
  var out = {warnings: []};
  if (req.doc) {
    var doc = openDoc(req.doc.id || state.doc_id);
    if (doc) {
      doc.setName(req.doc.title);
    } else {
      doc = DocumentApp.create(req.doc.title);
      var file = DriveApp.getFileById(doc.getId()).setDescription(DOC_MARK);
      state.doc_id = doc.getId(); // saved before the rest: a retry must find this Doc, not make another
      save();
      file.moveTo(folder(req.doc.folder));
    }
    renderMarkdown(doc.getBody(), req.doc.markdown);
    doc.saveAndClose();
    out.doc_id = doc.getId();
    out.doc_url = doc.getUrl();
  }
  // the e-mails don't hold the notes hostage: a failure is reported, the doc is kept
  if (req.recap && !state.recap_sent) {
    attempt(out, 'recap e-mail', function () {
      var r = req.recap;
      var link = out.doc_url ? '<p><a href="' + escapeHtml(out.doc_url) + '">' + escapeHtml(r.link_label) + '</a></p>\n' : '';
      var text = out.doc_url ? r.text + '\n\n' + r.link_label + ': ' + out.doc_url + '\n' : r.text;
      GmailApp.sendEmail(Session.getEffectiveUser().getEmail(), r.subject, text, {htmlBody: link + r.html, name: 'voicestream'});
      state.recap_sent = true;
      save();
    });
  }
  if (req.draft && !state.draft_id) {
    attempt(out, 'follow-up draft', function () {
      var d = req.draft;
      state.draft_id = GmailApp.createDraft(d.to.join(','), d.subject, d.text, {htmlBody: d.html}).getId();
      save();
    });
  }
  out.recap_sent = Boolean(state.recap_sent);
  out.draft_id = state.draft_id || null;
  return out;
}

function attempt(out, what, fn) {
  try {
    fn();
  } catch (err) {
    out.warnings.push(what + ': ' + ((err && err.message) || err));
  }
}

function openDoc(id) {
  if (!id) return null;
  try {
    var file = DriveApp.getFileById(id);
    // only a Doc this bridge made: a leaked secret must not be able to overwrite your other documents
    return file.isTrashed() || file.getDescription() !== DOC_MARK ? null : DocumentApp.openById(id);
  } catch (err) {
    if (/no item with the given id|missing|not found|deleted/i.test(String(err && err.message))) return null;
    throw err;
  }
}

function folder(name) {
  // only a folder you own: one with the same name that someone shared with you is not where your notes go
  var me = Session.getEffectiveUser().getEmail();
  var folders = DriveApp.getFoldersByName(name);
  while (folders.hasNext()) {
    var f = folders.next();
    var owner = f.getOwner();
    if (!f.isTrashed() && owner && owner.getEmail() === me) return f;
  }
  return DriveApp.createFolder(name);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ------------------------------------------------------- Markdown → Google Docs

var HEADING = /^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
var ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
var CHECK = /^\[([ xX])\]\s+/;
var RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
var FENCE = /^\s*(```|~~~)/;
var INLINE = new RegExp(
  [
    '`([^`\\n]+)`', // 1 code
    '\\[([^\\]\\n]+)\\]\\(([^()\\s]+)\\)', // 2 label, 3 url
    '\\*\\*(\\S(?:.*?\\S)?)\\*\\*|__(\\S(?:.*?\\S)?)__', // 4, 5 bold
    '~~(\\S(?:.*?\\S)?)~~', // 6 strikethrough
    '(^|[^\\w*])\\*([^\\s*](?:.*?[^\\s*])?)\\*(?![\\w*])', // 7 lead-in, 8 italic
    '(^|[^\\w])_([^\\s_](?:.*?[^\\s_])?)_(?!\\w)', // 9 lead-in, 10 italic
  ].join('|'),
  'g'
);

/** Markdown → blocks: {kind: heading|item|check|quote|code|para|rule, level, glyph, text}. */
function parseMarkdown(md) {
  var blocks = [];
  var fence = null;
  var prev = null; // the block an indented line continues
  String(md || '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .forEach(function (line) {
      var m;
      if (fence) {
        if (FENCE.test(line)) {
          blocks.push({kind: 'code', text: fence.join('\n')});
          fence = null;
        } else {
          fence.push(line);
        }
      } else if (FENCE.test(line)) {
        fence = [];
        prev = null;
      } else if (!line.trim()) {
        prev = null;
      } else if ((m = HEADING.exec(line))) {
        blocks.push({kind: 'heading', level: m[1].length, text: m[2]});
        prev = null;
      } else if (RULE.test(line)) {
        blocks.push({kind: 'rule'});
        prev = null;
      } else if (/^\s*>/.test(line)) {
        var quoted = line.replace(/^\s*>\s?/, '').trim();
        if (prev && prev.kind === 'quote') prev.text += '\n' + quoted;
        else blocks.push((prev = {kind: 'quote', text: quoted}));
      } else if ((m = ITEM.exec(line))) {
        var level = Math.min(8, Math.floor(m[1].replace(/\t/g, '    ').length / 2));
        var box = CHECK.exec(m[3]);
        blocks.push(
          (prev = box
            ? {kind: 'check', level: level, text: BOXES[box[1]] + ' ' + m[3].slice(box[0].length)}
            : {kind: 'item', level: level, glyph: /\d/.test(m[2]) ? 'NUMBER' : 'BULLET', text: m[3]})
        );
      } else if (prev && (prev.kind === 'para' || /^\s/.test(line))) {
        prev.text += '\n' + line.trim(); // a soft line break inside the same paragraph
      } else {
        blocks.push((prev = {kind: 'para', text: line.trim()}));
      }
    });
  if (fence) blocks.push({kind: 'code', text: fence.join('\n')});
  return blocks;
}

/** Inline Markdown → {text, marks: [{start, end, style, url}]}; offsets index ``text``. */
function parseInline(md) {
  var text = '';
  var marks = [];
  var last = 0;
  var m;
  INLINE.lastIndex = 0;
  while ((m = INLINE.exec(md))) {
    text += md.slice(last, m.index);
    var inner;
    var style = null;
    var url = null;
    if (m[1] !== undefined) {
      inner = m[1];
      style = 'code';
    } else if (m[2] !== undefined) {
      inner = m[2];
      if (SAFE_URL.test(m[3])) {
        style = 'link';
        url = m[3];
      }
    } else if (m[4] !== undefined || m[5] !== undefined) {
      inner = m[4] !== undefined ? m[4] : m[5];
      style = 'bold';
    } else if (m[6] !== undefined) {
      inner = m[6];
      style = 'strike';
    } else {
      text += m[7] !== undefined ? m[7] : m[9];
      inner = m[8] !== undefined ? m[8] : m[10];
      style = 'italic';
    }
    if (style) marks.push({start: text.length, end: text.length + inner.length, style: style, url: url});
    text += inner;
    last = INLINE.lastIndex;
  }
  return {text: text + md.slice(last), marks: marks};
}

function renderMarkdown(body, md) {
  body.clear(); // leaves one empty paragraph, removed at the end
  var list = null; // {item, glyph} heading the current list, so numbering continues
  parseMarkdown(md).forEach(function (b) {
    if (b.kind !== 'item') list = null;
    if (b.kind === 'rule') {
      body.appendHorizontalRule();
      return;
    }
    var span = b.kind === 'code' ? {text: b.text, marks: []} : parseInline(b.text);
    var el;
    if (b.kind === 'item') {
      el = body
        .appendListItem(span.text)
        .setNestingLevel(b.level)
        .setIndentStart(INDENT_PT * (b.level + 1))
        .setIndentFirstLine(INDENT_PT * (b.level + 0.5))
        .setGlyphType(DocumentApp.GlyphType[b.glyph]);
      if (list && !(b.level === 0 && b.glyph !== list.glyph)) el.setListId(list.item);
      else list = {item: el, glyph: b.glyph};
    } else {
      el = body.appendParagraph(span.text);
      if (b.kind === 'check') {
        el.setIndentFirstLine(INDENT_PT * b.level).setIndentStart(INDENT_PT * (b.level + 0.5));
      } else if (b.kind === 'quote') {
        el.setIndentStart(INDENT_PT).setIndentFirstLine(INDENT_PT);
        if (span.text) el.editAsText().setItalic(true);
      } else if (b.kind === 'code' && span.text) {
        el.editAsText().setFontFamily('Courier New');
      }
    }
    // explicit, since an appended paragraph can take on the previous one's heading
    el.setHeading(DocumentApp.ParagraphHeading[b.kind === 'heading' ? 'HEADING' + b.level : 'NORMAL']);
    var t = el.editAsText();
    span.marks.forEach(function (mk) {
      var end = mk.end - 1;
      if (mk.style === 'bold') t.setBold(mk.start, end, true);
      else if (mk.style === 'italic') t.setItalic(mk.start, end, true);
      else if (mk.style === 'strike') t.setStrikethrough(mk.start, end, true);
      else if (mk.style === 'code') t.setFontFamily(mk.start, end, 'Courier New');
      else if (mk.style === 'link') t.setLinkUrl(mk.start, end, mk.url);
    });
  });
  if (body.getNumChildren() > 1) body.getChild(0).removeFromParent();
}
