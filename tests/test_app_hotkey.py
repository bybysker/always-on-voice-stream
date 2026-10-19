"""The keyboard shortcut (voicestream/app/hotkey.py): its rules, its labels, the Carbon calls and the
keyboard layout's.

The Carbon part can only really run on a Mac (the packaged app's self-test does: it registers the
shortcut, presses it and reads the keyboard layout). Here it runs against a stand-in HIToolbox
(with UCKeyTranslate and the CoreFoundation calls) compiled from the prototypes in Apple's headers,
so what ctypes passes — the EventHotKeyID struct by value, the four-char codes, the double, the
out-pointers, the callback, the property keys read from their variables, the UTF-16 buffer — is
checked byte for byte.
"""

from __future__ import annotations

import ctypes
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from voicestream.app import hotkey

# ----------------------------------------------------------------- the rules


@pytest.mark.parametrize(
    ("code", "mods", "key", "why"),
    [
        ("KeyR", ["alt", "cmd"], "", None),                 # the default, ⌥⌘R
        ("KeyR", ["cmd", "alt", "cmd"], "", None),          # any order, repeats
        ("KeyK", ["ctrl", "alt"], "", None),
        ("KeyR", ["ctrl", "alt", "shift", "cmd"], "", None),
        ("F13", ["ctrl"], "", None),                        # a function key: one modifier is enough
        ("F5", ["ctrl", "alt"], "", None),
        ("F19", ["cmd", "shift"], "", None),
        ("F19", ["alt"], "", "option_only"),                # … but not ⌥ alone: macOS 15 can refuse it
        ("F19", ["alt", "shift"], "", "option_only"),
        ("Digit7", ["ctrl", "alt"], "", None),
        ("Space", ["ctrl", "shift", "cmd"], "", None),
        ("KeyR", [], "", "needs_modifier"),
        ("KeyR", ["shift"], "", "needs_modifier"),           # ⇧ alone still types it
        ("F5", [], "", "needs_modifier"),
        ("KeyC", ["cmd"], "", "one_modifier"),              # ⌘C copies
        ("KeyE", ["alt"], "", "one_modifier"),              # ⌥E types an accent
        ("Digit5", ["alt", "shift"], "", "one_modifier"),   # on a French keyboard ⌥⇧( types [
        ("KeyC", ["ctrl"], "", "one_modifier"),             # ⌃C stops a Terminal command
        ("Enter", ["cmd"], "", "one_modifier"),
        ("KeyQ", ["cmd"], "", "reserved_app"),              # Quit, before "one modifier": the better reason
        ("KeyW", ["cmd"], "", "reserved_app"),
        ("KeyH", ["cmd"], "", "reserved_app"),
        ("KeyM", ["cmd"], "", "reserved_app"),
        ("Comma", ["cmd"], "", "reserved_app"),
        ("KeyH", ["alt", "cmd"], "", "reserved_app"),       # Hide Others
        ("Tab", ["cmd"], "", "reserved_mac"),
        ("Backquote", ["cmd"], "", "reserved_mac"),
        ("Space", ["cmd"], "", "reserved_mac"),             # Spotlight
        ("Space", ["ctrl"], "", "reserved_mac"),            # input sources
        ("Escape", ["alt", "cmd"], "", "reserved_mac"),     # Force Quit
        ("KeyQ", ["ctrl", "cmd"], "", "reserved_mac"),      # Lock Screen
        ("Digit4", ["shift", "cmd"], "", "reserved_mac"),   # screenshots
        ("ArrowLeft", ["ctrl"], "", "reserved_mac"),        # Spaces
        ("F5", ["cmd"], "", "reserved_mac"),                # VoiceOver
        # keyboard navigation, all of it: keyboard access, the menu bar, the Dock, the next window, the
        # toolbar, the floating window, how Tab moves, the status menus (macOS keeps the press)
        *((f"F{n}", ["ctrl"], "", "reserved_mac") for n in range(1, 9)),
        ("Fn", ["alt", "cmd"], "", "unknown_key"),
        ("Numpad1", ["alt", "cmd"], "", "unknown_key"),
        ("MetaLeft", ["alt", "cmd"], "", "unknown_key"),
        # what a key types is what menus see: on AZERTY the key printed Q is KeyA, the one printed A is KeyQ
        ("KeyA", ["ctrl", "cmd"], "q", "reserved_mac"),     # ⌃⌘Q there
        ("KeyQ", ["ctrl", "cmd"], "a", None),               # ⌃⌘A there
        ("KeyM", ["cmd"], ",", "reserved_app"),             # AZERTY's comma: ⌘, is Settings
        ("Semicolon", ["cmd"], "m", "reserved_app"),        # AZERTY's M: ⌘M minimizes
    ],
)  # fmt: skip
def test_which_combinations_may_be_the_shortcut(code: str, mods: list[str], key: str, why: str | None) -> None:
    assert hotkey.problem(code, mods, key) == why
    if why is not None:
        assert hotkey.explain(why, code, mods, key)  # a sentence for the server's answer


def test_unknown_modifiers_are_refused() -> None:
    with pytest.raises(ValueError, match="not a modifier: hyper"):
        hotkey.problem("KeyR", ["alt", "hyper"])


def test_labels_and_the_macos_glyphs() -> None:
    assert hotkey.describe("KeyR", ["cmd", "alt"]) == "⌥⌘R"  # always ⌃⌥⇧⌘, as macOS writes them
    assert hotkey.describe("KeyK", ["shift", "cmd", "ctrl", "alt"]) == "⌃⌥⇧⌘K"
    assert hotkey.describe("KeyQ", ["ctrl", "cmd"], "a") == "⌃⌘A"  # AZERTY: what is printed on the key
    assert hotkey.describe("Space", ["ctrl", "alt"]) == "⌃⌥Space"
    for code, key, shown in (
        ("KeyR", "", "R"), ("KeyR", "r", "R"),          # a letter in capitals, as on the key
        ("KeyY", "z", "Z"),                             # QWERTZ: the key printed Z
        ("KeyM", ",", ","),                             # AZERTY: the key printed ,
        ("Minus", "ß", "ß"), ("KeyY", "ÿ", "Ÿ"),        # German: ß has no one-letter capital
        ("Digit1", "&", "1"), ("Digit1", "", "1"),      # AZERTY types & there: the digit is what is printed
        ("Comma", "", ","), ("Comma", ";", ";"), ("IntlBackslash", "", "§"),
        ("Enter", "Enter", "↩"), ("Backspace", "", "⌫"), ("Escape", "", "⎋"), ("Tab", "", "⇥"), ("ArrowUp", "", "↑"),
        ("F12", "F12", "F12"), ("Space", " ", "Space"),
    ):  # fmt: skip
        assert hotkey.label(code, key) == shown, (code, key)


def test_carbon_key_codes() -> None:
    """kVK_* from HIToolbox's Events.h; browsers derive KeyboardEvent.code from the same numbers
    (Chromium's dom_code_data.inc, Mac column)."""
    assert len(hotkey.KEY_CODES) == 26 + 10 + 19 + 12 + 9
    assert len(set(hotkey.KEY_CODES.values())) == len(hotkey.KEY_CODES)  # one key each
    assert all(f"Key{chr(c)}" in hotkey.KEY_CODES for c in range(ord("A"), ord("Z") + 1))
    assert all(f"Digit{d}" in hotkey.KEY_CODES for d in range(10)) and all(f"F{n}" in hotkey.KEY_CODES for n in range(1, 20))
    expected = {  # Events.h, kVK_ANSI_A = 0x00 …
        "KeyA": 0x00, "KeyS": 0x01, "KeyQ": 0x0C, "KeyW": 0x0D, "KeyR": 0x0F, "KeyO": 0x1F, "KeyM": 0x2E, "KeyN": 0x2D,
        "Digit1": 0x12, "Digit5": 0x17, "Digit6": 0x16, "Digit0": 0x1D, "Enter": 0x24, "Tab": 0x30, "Space": 0x31,
        "Backspace": 0x33, "Escape": 0x35, "Backquote": 0x32, "IntlBackslash": 0x0A, "Comma": 0x2B, "Slash": 0x2C,
        "F1": 0x7A, "F2": 0x78, "F5": 0x60, "F12": 0x6F, "F13": 0x69, "F17": 0x40, "F19": 0x50,
        "ArrowLeft": 0x7B, "ArrowRight": 0x7C, "ArrowDown": 0x7D, "ArrowUp": 0x7E,
    }  # fmt: skip
    assert {code: hotkey.KEY_CODES[code] for code in expected} == expected


def test_modifier_masks_for_carbon_and_appkit() -> None:
    # Events.h: cmdKey 0x0100, shiftKey 0x0200, optionKey 0x0800, controlKey 0x1000
    assert hotkey.carbon_combo("KeyR", ["alt", "cmd"]) == (0x0F, 0x0100 | 0x0800)
    assert hotkey.carbon_combo("F5", ["ctrl", "shift"]) == (0x60, 0x1000 | 0x0200)
    # NSEvent.h: NSEventModifierFlagShift 1 << 17, Control 1 << 18, Option 1 << 19, Command 1 << 20
    assert hotkey.appkit_mask(["alt", "cmd"]) == 0x180000
    assert hotkey.appkit_mask(["ctrl", "shift"]) == 0x060000
    assert hotkey.key_equivalent("KeyR") == "r" and hotkey.key_equivalent("KeyQ", "a") == "a"  # lower case: no ⇧
    assert hotkey.key_equivalent("Digit3", "\"") == "3" and hotkey.key_equivalent("Comma") == ","
    assert [hotkey.key_equivalent(c) for c in ("F1", "F19", "ArrowUp", "ArrowRight", "Enter", "Tab", "Backspace", "Escape",
                                                "Space")] == [  # NSEvent.h function keys, NSText.h characters
        "", "", "", "", "\r", "\t", "\x08", "\x1b", " "]  # fmt: skip


def test_carbon_constants_and_layouts() -> None:
    """CarbonEvents.h / CarbonEventsCore.h values; OSTypes are four characters, big-endian."""
    assert hotkey.KEYBOARD_CLASS == 0x6B657962  # 'keyb'
    assert hotkey.DIRECT_OBJECT == 0x2D2D2D2D  # '----'
    assert hotkey.HOT_KEY_ID_TYPE == 0x686B6964  # 'hkid'
    assert (hotkey.HOT_KEY_PRESSED, hotkey.EXCLUSIVE, hotkey.ATTRIBUTE_NONE) == (5, 1, 0)
    assert (hotkey.NO_ERR, hotkey.NOT_HANDLED, hotkey.HOT_KEY_EXISTS) == (0, -9874, -9878)
    assert ctypes.sizeof(hotkey.EventTypeSpec) == ctypes.sizeof(hotkey.EventHotKeyID) == 8  # two 32-bit fields
    assert [hotkey.status_of(e) for e in (0, -9878, -50)] == ["ok", "taken", "failed"]
    assert set(hotkey.TOGGLE) == {"stopped", "error", "listening", "paused"}  # starting, stopping: nothing


def test_off_macos_there_is_no_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hotkey, "_LIB", None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert hotkey.available() is False
    with pytest.raises(OSError, match="macOS"):
        hotkey.CarbonHotKey(lambda: None)
    monkeypatch.setattr(sys, "platform", "darwin")  # a Mac without HIToolbox where it should be
    monkeypatch.setattr(hotkey, "CARBON_PATHS", ("/nonexistent/HIToolbox",))
    assert hotkey.available() is False


# ------------------------------------------------- the Carbon calls, against a stand-in


FAKE_HITOOLBOX = r"""
/* What Voicestream calls in HIToolbox, with the prototypes of CarbonEvents.h and CarbonEventsCore.h
   and the types of MacTypes.h (LP64), recording what it is given. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef int32_t OSStatus;
typedef uint32_t UInt32, OSType, OptionBits, EventAttributes;
typedef unsigned long ItemCount, ByteCount;
typedef double EventTime;
typedef struct { OSType eventClass; UInt32 eventKind; } EventTypeSpec;
typedef struct { OSType signature; UInt32 id; } EventHotKeyID;
typedef OSStatus (*EventHandlerProcPtr)(void *inHandlerCallRef, void *inEvent, void *inUserData);

typedef struct { OSType cls; UInt32 kind; EventTime when; EventAttributes attrs; OSType name, type; ByteCount size;
                 unsigned char data[16]; } Event;

static int app_target, the_hot_key, call_ref;
static EventHandlerProcPtr handler;
static EventTypeSpec handled;
static int registered, released;
static UInt32 reg_code, reg_mods, reg_options, taken = 0xFFFFFFFF;
static EventHotKeyID reg_id;
static EventTime last_when = -1;

void *GetApplicationEventTarget(void) { return &app_target; }

OSStatus InstallEventHandler(void *inTarget, EventHandlerProcPtr inHandler, ItemCount inNumTypes,
                             const EventTypeSpec *inList, void *inUserData, void **outRef) {
    if (inTarget != &app_target || inNumTypes != 1 || !inHandler || !inList) return -50;  /* paramErr */
    handler = inHandler;
    handled = inList[0];
    if (outRef) *outRef = &handler;
    return 0;
}

OSStatus RemoveEventHandler(void *inHandlerRef) {
    if (inHandlerRef != &handler || !handler) return -50;
    handler = 0;
    return 0;
}

OSStatus RegisterEventHotKey(UInt32 inHotKeyCode, UInt32 inHotKeyModifiers, EventHotKeyID inHotKeyID,
                             void *inTarget, OptionBits inOptions, void **outRef) {
    if (inTarget != &app_target || !outRef) return -50;
    if (registered) return -9878;  /* Voicestream registers one shortcut at a time */
    if (inHotKeyCode == taken && (inOptions & 1)) return -9878;  /* another app holds it, exclusively */
    reg_code = inHotKeyCode; reg_mods = inHotKeyModifiers; reg_id = inHotKeyID; reg_options = inOptions;
    registered = 1;
    *outRef = &the_hot_key;
    return 0;
}

OSStatus UnregisterEventHotKey(void *inHotKey) {
    if (inHotKey != &the_hot_key || !registered) return -50;
    registered = 0;
    return 0;
}

OSStatus CreateEvent(void *inAllocator, OSType inClassID, UInt32 inKind, EventTime inWhen,
                     EventAttributes inAttributes, void **outEvent) {
    Event *e = calloc(1, sizeof *e);
    if (!e || !outEvent) return -108;
    e->cls = inClassID; e->kind = inKind; e->when = inWhen; e->attrs = inAttributes;
    last_when = inWhen;
    *outEvent = e;
    return 0;
}

OSStatus SetEventParameter(void *inEvent, OSType inName, OSType inType, ByteCount inSize, const void *inDataPtr) {
    Event *e = inEvent;
    if (inSize > sizeof e->data) return -50;
    e->name = inName; e->type = inType; e->size = inSize;
    memcpy(e->data, inDataPtr, inSize);
    return 0;
}

OSStatus GetEventParameter(void *inEvent, OSType inName, OSType inDesiredType, OSType *outActualType,
                           ByteCount inBufferSize, ByteCount *outActualSize, void *outData) {
    Event *e = inEvent;
    if (inName != e->name || inDesiredType != e->type) return -9870;  /* eventParameterNotFoundErr */
    if (inBufferSize < e->size) return -50;
    if (outActualType) *outActualType = e->type;
    if (outActualSize) *outActualSize = e->size;
    if (outData) memcpy(outData, e->data, e->size);
    return 0;
}

OSStatus SendEventToEventTarget(void *inEvent, void *inTarget) {
    Event *e = inEvent;
    if (inTarget != &app_target) return -50;
    if (!handler || e->cls != handled.eventClass || e->kind != handled.eventKind) return -9874;
    return handler(&call_ref, inEvent, 0);
}

void ReleaseEvent(void *inEvent) { free(inEvent); released++; }

/* the test's side */
int fake_registered(UInt32 *out) {
    out[0] = reg_code; out[1] = reg_mods; out[2] = reg_id.signature; out[3] = reg_id.id; out[4] = reg_options;
    return registered;
}
void fake_take(UInt32 code) { taken = code; }
int fake_released(void) { return released; }
double fake_last_when(void) { return last_when; }
UInt32 fake_handled(int kind) { return kind ? handled.eventKind : handled.eventClass; }

/* what macOS does when the registered combination is pressed, with the ID it was registered with
   (or another app's signature) */
OSStatus fake_press(int foreign) {
    void *e;
    EventHotKeyID id = reg_id;
    OSStatus r;
    if (!registered) return -50;
    if (foreign) id.signature = 'Othr';
    CreateEvent(0, 'keyb', 5, 12.5, 0, &e);
    SetEventParameter(e, '----', 'hkid', sizeof id, &id);
    r = SendEventToEventTarget(e, &app_target);
    ReleaseEvent(e);
    return r;
}

/* The keyboard layout: TextInputSources.h and Events.h (HIToolbox), UnicodeUtilities.h (CarbonCore),
   CFData.h, CFString.h and CFBase.h (CoreFoundation). The layout's 'uchr' data here is a table of what
   each virtual key code types with nothing held, in UTF-16. */
typedef uint8_t UInt8;
typedef uint16_t UInt16, UniChar;
typedef unsigned long UniCharCount;
typedef long CFIndex;
typedef unsigned char Boolean;

static int source, id_string, layout_data, id_key, uchr_key;
const void *const kTISPropertyInputSourceID = &id_key;  /* what the variables hold is the key */
const void *const kTISPropertyUnicodeKeyLayoutData = &uchr_key;
static UniChar layout_keys[128][2];
static int sources_alive, no_uchr, bad_calls, translations;
enum { KBD_TYPE = 43 };

void *TISCopyCurrentKeyboardLayoutInputSource(void) { sources_alive++; return &source; }

void *TISGetInputSourceProperty(void *inputSource, const void *propertyKey) {
    if (inputSource != &source || sources_alive < 1) { bad_calls++; return 0; }
    if (propertyKey == &id_key) return &id_string;
    if (propertyKey == &uchr_key) return no_uchr ? 0 : &layout_data;
    bad_calls++;
    return 0;
}

UInt8 LMGetKbdType(void) { return KBD_TYPE; }

const UInt8 *CFDataGetBytePtr(const void *theData) {
    if (theData != &layout_data) { bad_calls++; return 0; }
    return (const UInt8 *)layout_keys;
}

Boolean CFStringGetCString(const void *theString, char *buffer, CFIndex bufferSize, uint32_t encoding) {
    static const char id[] = "com.apple.keylayout.French";
    if (theString != &id_string || encoding != 0x08000100 || bufferSize < (CFIndex)sizeof id) return 0;
    memcpy(buffer, id, sizeof id);
    return 1;
}

void CFRelease(const void *cf) { if (cf == &source) sources_alive--; else bad_calls++; }

OSStatus UCKeyTranslate(const void *keyLayoutPtr, UInt16 virtualKeyCode, UInt16 keyAction, UInt32 modifierKeyState,
                        UInt32 keyboardType, OptionBits keyTranslateOptions, UInt32 *deadKeyState,
                        UniCharCount maxStringLength, UniCharCount *actualStringLength, UniChar unicodeString[]) {
    const UniChar *typed;
    translations++;
    if (keyLayoutPtr != (const void *)layout_keys || keyAction != 0 /* kUCKeyActionDown */ || modifierKeyState != 0
        || keyboardType != KBD_TYPE || !deadKeyState || *deadKeyState != 0 || maxStringLength < 2
        || !actualStringLength || !unicodeString || virtualKeyCode >= 128) {
        bad_calls++;
        return -50;  /* paramErr */
    }
    typed = layout_keys[virtualKeyCode];
    *actualStringLength = 0;
    if (typed[0] == '^' && !(keyTranslateOptions & 1)) { *deadKeyState = 1; return 0; }  /* a dead key: waits */
    for (int i = 0; i < 2 && typed[i]; i++) unicodeString[(*actualStringLength)++] = typed[i];
    return 0;
}

/* the test's side */
void fake_set_key(UInt16 code, UniChar first, UniChar second) { layout_keys[code][0] = first; layout_keys[code][1] = second; }
void fake_no_uchr(int on) { no_uchr = on; }
int fake_sources_alive(void) { return sources_alive; }
int fake_bad_calls(void) { return bad_calls; }
int fake_translations(void) { return translations; }
"""


@pytest.fixture
def fake_carbon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None or sys.platform == "win32":
        pytest.skip("needs a C compiler")
    src, lib = tmp_path / "hitoolbox.c", tmp_path / "libhitoolbox.so"
    src.write_text(FAKE_HITOOLBOX, encoding="utf-8")
    # -Wno-multichar: 'keyb' is how the headers write an OSType, packed big-endian as hotkey._fourcc does
    subprocess.run([cc, "-shared", "-fPIC", "-Wno-multichar", "-o", str(lib), str(src)], check=True, capture_output=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(hotkey, "CARBON_PATHS", ("/nonexistent/HIToolbox", str(lib)))  # the first one is missing
    monkeypatch.setattr(hotkey, "CORE_SERVICES_PATHS", ("/nonexistent/CoreServices", str(lib)))
    monkeypatch.setattr(hotkey, "CORE_FOUNDATION_PATHS", (str(lib),))
    monkeypatch.setattr(hotkey, "_LIB", None)
    monkeypatch.setattr(hotkey, "_LAYOUT_LIBS", None)
    fake = ctypes.CDLL(str(lib))
    fake.fake_last_when.restype = ctypes.c_double
    fake.fake_handled.restype = ctypes.c_uint32
    fake.fake_set_key.argtypes = [ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint16]
    return fake


def _registered(fake: Any) -> tuple[int, list[int]]:
    out = (ctypes.c_uint32 * 5)()
    return fake.fake_registered(out), list(out)


def test_the_carbon_calls(fake_carbon: Any) -> None:
    assert hotkey.available()
    pressed: list[str] = []
    hk = hotkey.CarbonHotKey(lambda: pressed.append("press"))
    assert hk.register("KeyR", ["cmd", "alt"]) == hotkey.NO_ERR
    assert hk.registered == ("KeyR", ("alt", "cmd"))
    # kEventClassKeyboard / kEventHotKeyPressed on the application target
    assert (fake_carbon.fake_handled(0), fake_carbon.fake_handled(1)) == (int.from_bytes(b"keyb", "big"), 5)
    on, (code, mods, signature, ident, options) = _registered(fake_carbon)
    assert on == 1 and (code, mods) == (0x0F, 0x0100 | 0x0800)  # kVK_ANSI_R, optionKey | cmdKey
    assert signature.to_bytes(4, "big") == b"VsHk" and ident == 1 and options == hotkey.EXCLUSIVE

    # a press, as macOS delivers it: our handler reads the EventHotKeyID and calls back
    assert fake_carbon.fake_press(0) == hotkey.NO_ERR and pressed == ["press"] and hk.presses == 1
    assert fake_carbon.fake_press(1) == hotkey.NOT_HANDLED and pressed == ["press"]  # another app's: not ours
    # the self-test's own press goes the same way
    released = fake_carbon.fake_released()
    assert hk.send_test_press() == hotkey.NO_ERR and pressed == ["press", "press"]
    assert fake_carbon.fake_released() == released + 1 and fake_carbon.fake_last_when() == 0.0  # 0: "now"

    # a new combination replaces the old one; one another app holds is refused, the old one is gone
    assert hk.register("KeyK", ["ctrl", "alt"]) == hotkey.NO_ERR and _registered(fake_carbon)[1][:2] == [0x28, 0x1800]
    fake_carbon.fake_take(0x11)  # kVK_ANSI_T
    assert hk.register("KeyT", ["ctrl", "alt"]) == hotkey.HOT_KEY_EXISTS and hk.registered is None
    assert _registered(fake_carbon)[0] == 0
    assert hk.register("F5", ["ctrl"]) == hotkey.NO_ERR and _registered(fake_carbon)[1][:2] == [0x60, 0x1000]

    def broken() -> None:
        raise RuntimeError("the host is gone")

    hk.on_press = broken  # an exception must not cross into C: logged, the press counts as handled
    assert fake_carbon.fake_press(0) == hotkey.NO_ERR and hk.presses == 3

    hk.close()
    assert _registered(fake_carbon)[0] == 0
    assert hk.send_test_press() == hotkey.NOT_HANDLED  # no handler any more
    hk.close()  # twice is fine


AZERTY = {  # what the keys of a French Mac keyboard type with nothing held, by physical key
    **{f"Key{c}": c.lower() for c in string.ascii_uppercase},
    "KeyQ": "a", "KeyA": "q", "KeyW": "z", "KeyZ": "w", "KeyM": ",", "Semicolon": "m", "Comma": ";", "Period": ":",
    "Slash": "=", "Minus": ")", "Equal": "-", "BracketLeft": "^", "BracketRight": "$", "Quote": "ù", "Backslash": "`",
    "Backquote": "<", "IntlBackslash": "@",
}  # fmt: skip


def test_the_keyboard_layout_calls(fake_carbon: Any) -> None:
    """What the keys type on the current layout, through TIS, UCKeyTranslate and CoreFoundation as their
    headers declare them: the property keys read from their variables, the layout's bytes, LMGetKbdType's
    keyboard type, no modifiers, no dead keys, UTF-16 out, the input source released."""
    for code, char in AZERTY.items():
        fake_carbon.fake_set_key(hotkey.KEY_CODES[code], ord(char), 0)
    ident, keys = hotkey.current_layout()
    assert ident == "com.apple.keylayout.French"
    assert keys == AZERTY  # ^ is a dead key there: without kUCKeyTranslateNoDeadKeysMask it would type nothing
    assert set(keys) == set(hotkey.LAYOUT_CODES) and fake_carbon.fake_translations() == len(hotkey.LAYOUT_CODES)
    assert fake_carbon.fake_bad_calls() == 0 and fake_carbon.fake_sources_alive() == 0
    assert hotkey.layout_problem(ident, keys) is None

    fake_carbon.fake_set_key(hotkey.KEY_CODES["Backquote"], 0xD834, 0xDD1E)  # one character beyond the BMP: two UniChars
    fake_carbon.fake_set_key(hotkey.KEY_CODES["Period"], 0x10, 0)  # a control character: not a label
    fake_carbon.fake_set_key(hotkey.KEY_CODES["IntlBackslash"], 0, 0)  # types nothing
    keys = hotkey.current_layout()[1]
    assert keys["Backquote"] == "\U0001d11e" and "Period" not in keys and "IntlBackslash" not in keys
    assert fake_carbon.fake_bad_calls() == 0 and fake_carbon.fake_sources_alive() == 0

    fake_carbon.fake_no_uchr(1)  # a layout with only 'KCHR' data
    with pytest.raises(OSError, match="com.apple.keylayout.French has no 'uchr' data"):
        hotkey.current_layout()
    assert fake_carbon.fake_sources_alive() == 0  # released all the same


def test_the_layout_check() -> None:
    """The self-test's: a character of its own for each letter key, and on a US layout the US one."""
    us = {code: hotkey.label(code).lower() for code in hotkey.LAYOUT_CODES}
    assert us["KeyQ"] == "q" and us["Semicolon"] == ";"
    assert hotkey.layout_problem("com.apple.keylayout.US", us) is None
    assert hotkey.layout_problem("com.apple.keylayout.ABC", us | {"Backquote": "§", "IntlBackslash": "`"}) is None  # ISO
    assert hotkey.layout_problem("com.apple.keylayout.French", AZERTY) is None
    assert hotkey.layout_problem("com.apple.keylayout.US", AZERTY) == (
        "com.apple.keylayout.US is a US layout, but: KeyA 'q', KeyM ',', KeyQ 'a', KeyW 'z', KeyZ 'w', Minus ')', "
        "Equal '-', BracketLeft '^', BracketRight '$', Backslash '`', Semicolon 'm', Quote 'ù', Comma ';', "
        "Period ':', Slash '='")
    assert hotkey.layout_problem("com.apple.keylayout.US", {k: v for k, v in us.items() if k != "KeyQ"}) == (
        "no character for KeyQ")
    assert hotkey.layout_problem("x", us | {"KeyW": "q"}) == "two letter keys type the same character"
    assert hotkey.layout_problem("x", us | {"KeyQ": "Q"}) == "a letter key types a capital with nothing held"
    assert hotkey.layout_problem("x", {}) is not None


def test_the_layout_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hotkey, "_LAYOUT_LIBS", None)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError, match="macOS"):
        hotkey.current_layout()


@pytest.mark.skipif(sys.platform != "darwin", reason="the real keyboard layout")
def test_the_real_keyboard_layout_on_a_mac() -> None:
    """On a Mac (the macOS CI jobs, whose layout is a US one: KeyQ types q there), in its own process so a
    wrong signature can only fail this test."""
    code = (
        "from voicestream.app import hotkey\n"
        "ident, keys = hotkey.current_layout()\n"
        "print(ident, keys)\n"
        "problem = hotkey.layout_problem(ident, keys)\n"
        "assert problem is None, problem\n"
        "assert ident not in hotkey.US_LAYOUTS or keys['KeyQ'] == 'q'\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), (out.stdout, out.stderr)


@pytest.mark.skipif(sys.platform != "darwin", reason="the real HIToolbox")
def test_the_real_carbon_handler_on_a_mac() -> None:
    """On a Mac (the macOS CI jobs): the handler installs on the real application target and a synthetic
    press reaches it. In its own process, so a wrong signature can only fail this test. Registering a
    combination needs the window server: the packaged app's self-test does that."""
    code = (
        "from voicestream.app import hotkey\n"
        "presses = []\n"
        "hk = hotkey.CarbonHotKey(lambda: presses.append(1))\n"
        "assert hk.install() == hotkey.NO_ERR\n"
        "assert hk.send_test_press() == hotkey.NO_ERR and presses == [1]\n"
        "hk.close()\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr
