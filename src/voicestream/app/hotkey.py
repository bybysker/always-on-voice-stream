"""The keyboard shortcut that starts and stops listening from any app (⌥⌘R unless you change it).

A shortcut is a physical key, named as the page's ``KeyboardEvent.code`` names it (``"KeyR"``: the
same place on a QWERTY, AZERTY or QWERTZ keyboard), its modifiers, and what is printed on the key
there (``KeyboardEvent.key``): that is what is shown, and what tells which menu command (⌘Q…) a
combination would take, as menus go by the character a key types. The pure part of this module —
which keys and combinations may be used, how they are written with the macOS glyphs, what Carbon
and AppKit want for them — runs and is tested everywhere; the app's preferences (:mod:`.prefs`)
and the Settings page (``app.js``, which keeps the same rules: keep the two in step) rely on it.

The Mac part, :class:`CarbonHotKey`, registers the shortcut with Carbon's hot key API
(``RegisterEventHotKey`` in HIToolbox) through ``ctypes``: unlike an ``NSEvent`` global monitor it
needs neither the Accessibility nor the Input Monitoring permission, and it is still how macOS
offers a global shortcut to an app. The native shell (:mod:`.native`) uses it on the main thread
only (Carbon's event calls are not thread safe) and keeps it for the life of the app: Carbon holds
on to the raw pointer of the ctypes callback, not to the Python object.

With ⌥ held a Mac's ``KeyboardEvent.key`` is the character ⌥ types (⌥A is æ on a French keyboard),
which says nothing of what is printed on the key: :func:`current_layout` asks macOS what each key
types with nothing held on the keyboard layout in use (``UCKeyTranslate`` on its 'uchr' data), and
the shell hands that to the page and to the preferences' checks (main thread only too).

Every constant and signature below comes from the macOS SDK headers, quoted by name: HIToolbox's
``CarbonEvents.h``, ``CarbonEventsCore.h``, ``Events.h`` and ``TextInputSources.h``
(``Carbon.framework/Frameworks/HIToolbox.framework/Headers``), CarbonCore's ``UnicodeUtilities.h``
(``CoreServices.framework/Frameworks/CarbonCore.framework/Headers``), CoreFoundation's ``CFBase.h``,
``CFData.h`` and ``CFString.h``, ``MacTypes.h`` for the base types, and AppKit's ``NSEvent.h`` and
``NSText.h`` for the menu.
"""

from __future__ import annotations

import ctypes
import logging
import string
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

log = logging.getLogger(__name__)

MODIFIERS = ("ctrl", "alt", "shift", "cmd")
"""In the order macOS writes them: ⌃⌥⇧⌘."""
GLYPHS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
DEFAULT_CODE, DEFAULT_MODS = "KeyR", ("alt", "cmd")  # ⌥⌘R: R for record

TOGGLE = {"stopped": "start", "error": "restart", "listening": "stop", "paused": "resume"}
"""What a press does in each state (:meth:`voicestream.app.host.AppHost.meeting_toggle`); starting, it
does nothing, and stopping, it starts the next meeting once the last one is wrapped up — unless it comes within
moments of the Stop that began that wrap-up: then it is the second half of a double press, part of that Stop."""

# ------------------------------------------------------------------ the keys

# Carbon's virtual key codes (Events.h, kVK_*): a key's place on the keyboard, like KeyboardEvent.code.
# Browsers turn the same codes into KeyboardEvent.code (Chromium's dom_code_data.inc, its Mac column,
# has the same values), so this table is that one the other way round.
_LETTERS = {
    "A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06, "X": 0x07, "C": 0x08,
    "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E, "R": 0x0F, "Y": 0x10, "T": 0x11, "O": 0x1F,
    "U": 0x20, "I": 0x22, "P": 0x23, "L": 0x25, "J": 0x26, "K": 0x28, "N": 0x2D, "M": 0x2E,
}  # kVK_ANSI_A … kVK_ANSI_M  # fmt: skip
_DIGITS = {"1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A, "8": 0x1C,
           "0": 0x1D}  # kVK_ANSI_0 … kVK_ANSI_9  # fmt: skip
_FUNCTION = {
    1: 0x7A, 2: 0x78, 3: 0x63, 4: 0x76, 5: 0x60, 6: 0x61, 7: 0x62, 8: 0x64, 9: 0x65, 10: 0x6D, 11: 0x67,
    12: 0x6F, 13: 0x69, 14: 0x6B, 15: 0x71, 16: 0x6A, 17: 0x40, 18: 0x4F, 19: 0x50,
}  # kVK_F1 … kVK_F19  # fmt: skip
PUNCTUATION = {
    # code: (kVK_*, what a US keyboard prints on it, shown when the real label isn't known)
    "Minus": (0x1B, "-"),            # kVK_ANSI_Minus
    "Equal": (0x18, "="),            # kVK_ANSI_Equal
    "BracketLeft": (0x21, "["),      # kVK_ANSI_LeftBracket
    "BracketRight": (0x1E, "]"),     # kVK_ANSI_RightBracket
    "Backslash": (0x2A, "\\"),       # kVK_ANSI_Backslash
    "Semicolon": (0x29, ";"),        # kVK_ANSI_Semicolon
    "Quote": (0x27, "'"),            # kVK_ANSI_Quote
    "Comma": (0x2B, ","),            # kVK_ANSI_Comma
    "Period": (0x2F, "."),           # kVK_ANSI_Period
    "Slash": (0x2C, "/"),            # kVK_ANSI_Slash
    "Backquote": (0x32, "`"),        # kVK_ANSI_Grave
    "IntlBackslash": (0x0A, "§"),    # kVK_ISO_Section: the extra key of ISO (European) keyboards
}  # fmt: skip
SPECIAL = {
    # code: (kVK_*, how macOS draws it)
    "Space": (0x31, "Space"),        # kVK_Space
    "Enter": (0x24, "↩"),            # kVK_Return
    "Tab": (0x30, "⇥"),              # kVK_Tab
    "Backspace": (0x33, "⌫"),        # kVK_Delete (the key above Return)
    "Escape": (0x35, "⎋"),           # kVK_Escape
    "ArrowLeft": (0x7B, "←"),        # kVK_LeftArrow
    "ArrowRight": (0x7C, "→"),       # kVK_RightArrow
    "ArrowDown": (0x7D, "↓"),        # kVK_DownArrow
    "ArrowUp": (0x7E, "↑"),          # kVK_UpArrow
}  # fmt: skip
KEY_CODES: dict[str, int] = {
    **{f"Key{k}": v for k, v in _LETTERS.items()},
    **{f"Digit{d}": v for d, v in _DIGITS.items()},
    **{f"F{n}": v for n, v in _FUNCTION.items()},
    **{code: v for code, (v, _) in PUNCTUATION.items()},
    **{code: v for code, (v, _) in SPECIAL.items()},
}
"""``KeyboardEvent.code`` → Carbon virtual key code, for every key a shortcut may use."""

CARBON_MODIFIERS = {"cmd": 1 << 8, "shift": 1 << 9, "alt": 1 << 11, "ctrl": 1 << 12}
"""Events.h: ``cmdKey = 1 << cmdKeyBit`` (8), ``shiftKey`` (9), ``optionKey`` (11), ``controlKey`` (12)."""
APPKIT_MODIFIERS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}
"""NSEvent.h ``NSEventModifierFlags``: ``…Shift = 1 << 17``, ``…Control``, ``…Option``, ``…Command = 1 << 20``."""

# what an NSMenuItem's keyEquivalent is for the keys that print no character: NSEvent.h
# (NSUpArrowFunctionKey = 0xF700 …, NSF1FunctionKey = 0xF704 …) and NSText.h (NSCarriageReturnCharacter
# 0x0d, NSTabCharacter 0x09, NSBackspaceCharacter 0x08); Escape and Space are their own characters
_MENU_KEYS = {"Enter": "\r", "Tab": "\t", "Backspace": "\x08", "Escape": "\x1b", "Space": " ",
              "ArrowUp": chr(0xF700), "ArrowDown": chr(0xF701), "ArrowLeft": chr(0xF702), "ArrowRight": chr(0xF703)}

RESERVED: dict[str, str] = {
    # what every Mac app does with these: Quit, Close, Hide, Minimize, Settings (and their ⌥ variants)
    **dict.fromkeys(("cmd+KeyQ", "cmd+KeyW", "cmd+KeyH", "cmd+KeyM", "cmd+Comma", "alt+cmd+KeyH", "alt+cmd+KeyW",
                     "alt+cmd+KeyM"), "app"),
    # what macOS itself does with these
    **dict.fromkeys((
        "cmd+Tab", "shift+cmd+Tab",                                    # switch apps
        "cmd+Backquote", "shift+cmd+Backquote", "cmd+IntlBackslash", "shift+cmd+IntlBackslash",  # switch windows
        "cmd+Space", "alt+cmd+Space",                                  # Spotlight, Finder search
        "ctrl+Space", "ctrl+alt+Space", "ctrl+cmd+Space",              # input sources, Emoji & Symbols
        "alt+cmd+Escape",                                              # Force Quit
        "ctrl+cmd+KeyQ", "shift+cmd+KeyQ", "alt+shift+cmd+KeyQ",       # lock the screen, log out
        "shift+cmd+Digit3", "shift+cmd+Digit4", "shift+cmd+Digit5", "shift+cmd+Digit6",
        "ctrl+shift+cmd+Digit3", "ctrl+shift+cmd+Digit4",              # screenshots
        "ctrl+cmd+KeyF", "alt+cmd+KeyD",                               # full screen, the Dock
        "ctrl+ArrowLeft", "ctrl+ArrowRight", "ctrl+ArrowUp", "ctrl+ArrowDown",  # Spaces, Mission Control
        "cmd+F5", "alt+cmd+F5",                                        # VoiceOver, accessibility
        # keyboard navigation (Keyboard Shortcuts → Keyboard): keyboard access, focus on the menu bar,
        # the Dock, the next window, the toolbar, the floating window, how Tab moves, the status menus
        "ctrl+F1", "ctrl+F2", "ctrl+F3", "ctrl+F4", "ctrl+F5", "ctrl+F6", "ctrl+F7", "ctrl+F8",
    ), "mac"),
}  # fmt: skip
"""Combinations taken already, by their :func:`combo_id`: ``"app"`` (every app's own commands) or
``"mac"`` (macOS's). Menu commands (⌘Q, ⌃⌘Q…) follow the character a key types, macOS's own
shortcuts (⌘Space, ⇧⌘3…) the key: :func:`problem` looks a letter or punctuation key up by its
label, so ⌘ with the key printed Q on an AZERTY keyboard is ⌘Q there."""

REASONS = {
    "unknown_key": "Voicestream can't use this key for a shortcut: choose a letter, a digit, a function key "
                   "or a punctuation key",
    "needs_modifier": "Add ⌘, ⌥ or ⌃: on its own (or with ⇧ only) the key types or does something else",
    "one_modifier": "{combo} is taken in many apps already (⌘C copies, ⌥E types an accent, ⌃C stops a Terminal "
                    "command): add a second one of ⌘, ⌥ or ⌃, as in ⌥⌘R",
    "reserved_app": "{combo} is a command every Mac app has (Quit, Close, Hide, Minimize, Settings): choose another",
    "reserved_mac": "macOS uses {combo} itself (switching apps or windows, Spotlight, input sources, screenshots, "
                    "Force Quit…): choose another",
    "option_only": "macOS 15 can refuse a shortcut held with ⌥ alone, or ⌥⇧ ({combo}): add ⌘ or ⌃",
}  # fmt: skip
"""Why a combination is refused, for the server's answer (the page has its own words, in two languages)."""


def normalize_mods(mods: Iterable[str]) -> list[str]:
    """``mods`` once each, in macOS's order; raises ``ValueError`` for a name that isn't a modifier."""
    given = set(mods)
    unknown = given - set(MODIFIERS)
    if unknown:
        raise ValueError(f"not a modifier: {', '.join(sorted(unknown))}")
    return [m for m in MODIFIERS if m in given]


def combo_id(code: str, mods: Iterable[str]) -> str:
    """``"alt+cmd+KeyR"``: how :data:`RESERVED` (and the page) name a combination."""
    return "+".join([*normalize_mods(mods), code])


def is_function_key(code: str) -> bool:
    return code.startswith("F") and code[1:].isdigit() and code in KEY_CODES


_BY_CHARACTER = {char: code for code, (_, char) in PUNCTUATION.items()}


def problem(code: str, mods: Iterable[str], key: str = "") -> str | None:
    """Why ``code`` with ``mods`` can't be the shortcut (a key of :data:`REASONS`), or ``None``.
    ``key``: the key's label on this keyboard, as for :func:`label`.

    A key needs ⌘, ⌥ or ⌃ (⇧ alone still types it), and two of them unless it is a function key:
    with only one, apps already use the combination for themselves — ⌘C copies, ⌥E types an accent
    (on a French keyboard ⌥( types {), ⌃C stops a command in Terminal — and a global shortcut would
    take it away from every one of them. Some combinations with two are macOS's own (:data:`RESERVED`).
    macOS 15 may refuse ⌥ or ⌥⇧ alone even with a function key."""
    mods = normalize_mods(mods)
    if code not in KEY_CODES:
        return "unknown_key"
    strong = [m for m in mods if m != "shift"]
    if not strong:
        return "needs_modifier"
    reserved = RESERVED.get(combo_id(typed(code, key), mods))
    if reserved:
        return "reserved_" + reserved
    if len(strong) == 1 and not is_function_key(code):
        return "one_modifier"
    if strong == ["alt"]:
        # a function key with ⌥ or ⌥⇧ only: macOS 15.0 and 15.1 refused those to every app (against key
        # loggers); Apple said 15.2 allows them again, but people still report them failing
        return "option_only"
    return None


def typed(code: str, key: str = "") -> str:
    """The code of the key a US keyboard has for what this one types (``KeyQ`` for the AZERTY key printed
    Q, physically ``KeyA``); other keys are their own."""
    if not (code.startswith("Key") or code in PUNCTUATION):
        return code
    char = label(code, key)
    if len(char) == 1 and "A" <= char <= "Z":
        return "Key" + char
    return _BY_CHARACTER.get(char, code)


def label(code: str, key: str = "") -> str:
    """What to print for the key: the label it has on this keyboard (``key``, from the page) when that
    is a plain character, else the one macOS draws or a US keyboard prints. A digit is always its digit
    (``key`` may be the character above it); a letter is in capitals, as on the key."""
    key = (key or "").strip()
    if (code.startswith("Key") or code in PUNCTUATION) and len(key) == 1 and key.isprintable():
        upper = key.upper()
        return upper if len(upper) == 1 else key  # the German ß is ß on the key, not SS
    if code.startswith(("Key", "Digit")):
        return code.removeprefix("Key").removeprefix("Digit")
    if code in PUNCTUATION:
        return PUNCTUATION[code][1]
    if code in SPECIAL:
        return SPECIAL[code][1]
    return code  # F1 … F19


def describe(code: str, mods: Iterable[str], key: str = "") -> str:
    """``⌥⌘R``: the glyphs in macOS's order, then the key."""
    return "".join(GLYPHS[m] for m in normalize_mods(mods)) + label(code, key)


def explain(reason: str, code: str, mods: Iterable[str], key: str = "") -> str:
    return REASONS[reason].format(combo=describe(code, mods, key))


def carbon_combo(code: str, mods: Iterable[str]) -> tuple[int, int]:
    """``(virtual key code, modifier mask)`` for ``RegisterEventHotKey``."""
    mask = 0
    for m in normalize_mods(mods):
        mask |= CARBON_MODIFIERS[m]
    return KEY_CODES[code], mask


def appkit_mask(mods: Iterable[str]) -> int:
    """``NSMenuItem.keyEquivalentModifierMask`` for ``mods``."""
    mask = 0
    for m in normalize_mods(mods):
        mask |= APPKIT_MODIFIERS[m]
    return mask


def key_equivalent(code: str, key: str = "") -> str:
    """``NSMenuItem.keyEquivalent``: the character AppKit draws (and matches) for the key, in lower
    case — a capital would mean ⇧ — or the function-key character of a key that prints none."""
    if code in _MENU_KEYS:
        return _MENU_KEYS[code]
    if is_function_key(code):
        return chr(0xF704 + int(code[1:]) - 1)  # NSF1FunctionKey = 0xF704, then one per key up to F35
    return label(code, key).lower()


# ------------------------------------------------------------------- Carbon


def _fourcc(text: bytes) -> int:
    """An ``OSType`` (``'keyb'`` in C): the four bytes read as a big-endian number."""
    return int.from_bytes(text, "big")


NO_ERR = 0  # MacTypes.h noErr
NOT_HANDLED = -9874  # CarbonEventsCore.h eventNotHandledErr
HOT_KEY_EXISTS = -9878
"""CarbonEventsCore.h ``eventHotKeyExistsErr``: this process has the combination already, or another
app registered it with ``kEventHotKeyExclusive`` too."""
KEYBOARD_CLASS = _fourcc(b"keyb")  # CarbonEvents.h kEventClassKeyboard = 'keyb'
HOT_KEY_PRESSED = 5  # CarbonEvents.h kEventHotKeyPressed (in kEventClassKeyboard)
DIRECT_OBJECT = _fourcc(b"----")  # CarbonEventsCore.h kEventParamDirectObject = '----'
HOT_KEY_ID_TYPE = _fourcc(b"hkid")  # CarbonEvents.h typeEventHotKeyID = 'hkid'
EXCLUSIVE = 1 << 0
"""CarbonEvents.h ``kEventHotKeyExclusive`` (macOS 10.5+): while we hold the combination, another app's
non-exclusive registration of it is not told about presses (one key, one action), and an app that
holds it exclusively already makes ours fail with :data:`HOT_KEY_EXISTS` rather than share it."""
ATTRIBUTE_NONE = 0  # CarbonEventsCore.h kEventAttributeNone
SIGNATURE = _fourcc(b"VsHk")  # EventHotKeyID.signature: any four characters of ours
HOT_KEY_ID = 1  # EventHotKeyID.id: there is only the one shortcut

CARBON_PATHS = (
    "/System/Library/Frameworks/Carbon.framework/Versions/A/Frameworks/HIToolbox.framework/Versions/A/HIToolbox",
    "/System/Library/Frameworks/Carbon.framework/Carbon",  # the umbrella, which re-exports HIToolbox
)
"""dlopen() finds these in the system's shared cache even where the files aren't on disk (macOS 11+)."""


class EventTypeSpec(ctypes.Structure):
    """CarbonEventsCore.h: ``struct EventTypeSpec { OSType eventClass; UInt32 eventKind; }``."""

    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class EventHotKeyID(ctypes.Structure):
    """CarbonEvents.h: ``struct EventHotKeyID { OSType signature; UInt32 id; }`` (MacTypes.h: OSType is a
    FourCharCode, a UInt32; UInt32 is an ``unsigned int`` on 64-bit)."""

    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


# CarbonEventsCore.h: typedef OSStatus (*EventHandlerProcPtr)(EventHandlerCallRef inHandlerCallRef,
# EventRef inEvent, void *inUserData); on Mach-O an EventHandlerUPP is that same pointer (the header's
# NewEventHandlerUPP is a cast there). OSStatus is an SInt32.
HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_LIB: Any = None  # HIToolbox, once loaded by carbon()


def _declare(lib: Any) -> Any:
    """The prototypes, as the headers give them. MacTypes.h: OSStatus SInt32, OSType/UInt32/OptionBits/
    EventAttributes 32-bit unsigned, ItemCount and ByteCount ``unsigned long``, EventTime a ``double``;
    every *Ref is a pointer."""
    p, u32, out = ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
    status = ctypes.c_int32
    for name, restype, argtypes in (
        # CarbonEvents.h: EventTargetRef GetApplicationEventTarget(void)
        ("GetApplicationEventTarget", p, []),
        # CarbonEventsCore.h: OSStatus InstallEventHandler(EventTargetRef inTarget, EventHandlerUPP inHandler,
        #   ItemCount inNumTypes, const EventTypeSpec *inList, void *inUserData, EventHandlerRef *outRef)
        ("InstallEventHandler", status, [p, HANDLER, ctypes.c_ulong, ctypes.POINTER(EventTypeSpec), p, out]),
        # CarbonEventsCore.h: OSStatus RemoveEventHandler(EventHandlerRef inHandlerRef)
        ("RemoveEventHandler", status, [p]),
        # CarbonEvents.h: OSStatus RegisterEventHotKey(UInt32 inHotKeyCode, UInt32 inHotKeyModifiers,
        #   EventHotKeyID inHotKeyID, EventTargetRef inTarget, OptionBits inOptions, EventHotKeyRef *outRef)
        ("RegisterEventHotKey", status, [u32, u32, EventHotKeyID, p, u32, out]),
        # CarbonEvents.h: OSStatus UnregisterEventHotKey(EventHotKeyRef inHotKey)
        ("UnregisterEventHotKey", status, [p]),
        # CarbonEventsCore.h: OSStatus GetEventParameter(EventRef inEvent, EventParamName inName,
        #   EventParamType inDesiredType, EventParamType *outActualType, ByteCount inBufferSize,
        #   ByteCount *outActualSize, void *outData)
        ("GetEventParameter", status, [p, u32, u32, ctypes.POINTER(u32), ctypes.c_ulong,
                                       ctypes.POINTER(ctypes.c_ulong), p]),
        # CarbonEventsCore.h: OSStatus [Mac]CreateEvent(CFAllocatorRef inAllocator, OSType inClassID,
        #   UInt32 inKind, EventTime inWhen, EventAttributes inAttributes, EventRef *outEvent)
        #   (MacCreateEvent is #defined to CreateEvent on the Mac: that is the symbol)
        ("CreateEvent", status, [p, u32, u32, ctypes.c_double, u32, out]),
        # CarbonEventsCore.h: OSStatus SetEventParameter(EventRef inEvent, EventParamName inName,
        #   EventParamType inType, ByteCount inSize, const void *inDataPtr)
        ("SetEventParameter", status, [p, u32, u32, ctypes.c_ulong, p]),
        # CarbonEventsCore.h: OSStatus SendEventToEventTarget(EventRef inEvent, EventTargetRef inTarget)
        ("SendEventToEventTarget", status, [p, p]),
        # CarbonEventsCore.h: void ReleaseEvent(EventRef inEvent)
        ("ReleaseEvent", None, [p]),
    ):  # fmt: skip
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    return lib


def _open(paths: Iterable[str], declare: Callable[[Any], Any]) -> Any:
    """``declare(library)`` for the first of ``paths`` that loads; raises ``OSError`` saying why none did."""
    errors = []
    for path in paths:
        try:
            return declare(ctypes.CDLL(path))
        except (OSError, AttributeError, ValueError) as exc:  # AttributeError, ValueError: a symbol is missing
            errors.append(f"{path}: {exc}")
    raise OSError("; ".join(errors))


def carbon() -> Any:
    """HIToolbox with the hot key calls declared (loaded once). Raises ``OSError`` off macOS, or where
    it can't be loaded."""
    global _LIB
    if _LIB is None:
        if sys.platform != "darwin":
            raise OSError("global keyboard shortcuts are a macOS feature")
        _LIB = _open(CARBON_PATHS, _declare)
    return _LIB


def available() -> bool:
    """A shortcut can be registered here (macOS, with Carbon's HIToolbox)."""
    try:
        carbon()
    except OSError as exc:
        log.debug("no global keyboard shortcut: %s", exc)
        return False
    return True


def status_of(err: int) -> str:
    """``RegisterEventHotKey``'s answer in the words the page uses: ``"ok"`` (works from any app),
    ``"taken"`` (another app has it) or ``"failed"``."""
    return "ok" if err == NO_ERR else "taken" if err == HOT_KEY_EXISTS else "failed"


class CarbonHotKey:
    """One global shortcut. Main thread only; keep the object for as long as the shortcut may fire.

    ``on_press()`` is called on the main thread (from the app's event loop) at each press."""

    def __init__(self, on_press: Callable[[], None]) -> None:
        self.on_press = on_press
        self.presses = 0
        self.registered: tuple[str, tuple[str, ...]] | None = None
        self._lib = carbon()
        self._callback = HANDLER(self._handle)  # Carbon keeps only its address: this keeps it alive
        self._handler = ctypes.c_void_p()  # EventHandlerRef
        self._ref = ctypes.c_void_p()  # EventHotKeyRef

    def _target(self) -> Any:
        return self._lib.GetApplicationEventTarget()

    def install(self) -> int:
        """Listen for kEventClassKeyboard / kEventHotKeyPressed on the application (once)."""
        if self._handler.value:
            return NO_ERR
        spec = (EventTypeSpec * 1)(EventTypeSpec(KEYBOARD_CLASS, HOT_KEY_PRESSED))
        return int(self._lib.InstallEventHandler(self._target(), self._callback, 1, spec, None, ctypes.byref(self._handler)))

    def register(self, code: str, mods: Iterable[str]) -> int:
        """Make ``code`` + ``mods`` the shortcut (replacing the previous one); returns the OSStatus."""
        mods = tuple(normalize_mods(mods))
        self.unregister()
        err = self.install()
        if err != NO_ERR:
            return err
        key, mask = carbon_combo(code, mods)
        ref = ctypes.c_void_p()
        err = int(self._lib.RegisterEventHotKey(key, mask, EventHotKeyID(SIGNATURE, HOT_KEY_ID), self._target(),
                                                EXCLUSIVE, ctypes.byref(ref)))
        if err == NO_ERR:
            self._ref, self.registered = ref, (code, mods)
        return err

    def unregister(self) -> None:
        if self._ref.value:
            err = self._lib.UnregisterEventHotKey(self._ref)
            if err != NO_ERR:
                log.warning("could not remove the keyboard shortcut (error %s)", err)
        self._ref, self.registered = ctypes.c_void_p(), None

    def close(self) -> None:
        """Quitting: the shortcut and the handler go (macOS would also drop them with the process)."""
        self.unregister()
        if self._handler.value:
            self._lib.RemoveEventHandler(self._handler)
            self._handler = ctypes.c_void_p()

    def send_test_press(self) -> int:
        """The self-test's press: a kEventHotKeyPressed event with our EventHotKeyID, sent to the
        application target as macOS sends a real one, so our handler (and what it calls) runs now.
        Returns SendEventToEventTarget's OSStatus (our handler's answer)."""
        event = ctypes.c_void_p()
        err = int(self._lib.CreateEvent(None, KEYBOARD_CLASS, HOT_KEY_PRESSED, 0.0, ATTRIBUTE_NONE, ctypes.byref(event)))
        if err != NO_ERR:
            return err
        try:
            hk = EventHotKeyID(SIGNATURE, HOT_KEY_ID)
            err = int(self._lib.SetEventParameter(event, DIRECT_OBJECT, HOT_KEY_ID_TYPE, ctypes.sizeof(hk), ctypes.byref(hk)))
            if err != NO_ERR:
                return err
            return int(self._lib.SendEventToEventTarget(event, self._target()))
        finally:
            self._lib.ReleaseEvent(event)

    def _handle(self, call_ref: Any, event: Any, user_data: Any) -> int:
        """Carbon's handler: a press of a hot key of this process."""
        try:
            hk = EventHotKeyID()
            err = self._lib.GetEventParameter(event, DIRECT_OBJECT, HOT_KEY_ID_TYPE, None, ctypes.sizeof(hk), None,
                                              ctypes.byref(hk))
            if err != NO_ERR or hk.signature != SIGNATURE:
                return NOT_HANDLED  # not ours: let another handler have it
            self.presses += 1
            self.on_press()
        except Exception:  # an exception must not cross into C
            log.exception("the keyboard shortcut failed")
        return NO_ERR


# ------------------------------------------------------------ the keyboard layout

LAYOUT_CODES = (*(f"Key{c}" for c in string.ascii_uppercase), *PUNCTUATION)
"""The keys whose label depends on the layout: a digit is always shown as its digit, the others as
macOS draws them."""
US_LAYOUTS = frozenset({"com.apple.keylayout.US", "com.apple.keylayout.ABC"})
"""Layouts (``kTISPropertyInputSourceID``) whose letter and punctuation keys type what a US keyboard
prints on them, as on the macOS CI runners."""

CORE_SERVICES_PATHS = (
    "/System/Library/Frameworks/CoreServices.framework/CoreServices",  # the umbrella, which re-exports CarbonCore
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/CarbonCore.framework/CarbonCore",
)
CORE_FOUNDATION_PATHS = ("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",)
KEY_ACTION_DOWN = 0  # UnicodeUtilities.h kUCKeyActionDown: the character the key types going down
NO_DEAD_KEYS = 1 << 0
"""UnicodeUtilities.h ``kUCKeyTranslateNoDeadKeysMask`` (``1L << kUCKeyTranslateNoDeadKeysBit``, 0): a
dead key (^ on a French keyboard) gives its own character rather than waiting for the next key."""
UTF8 = 0x08000100  # CFString.h kCFStringEncodingUTF8
MAX_CHARS = 4  # UCKeyTranslate's buffer, in UniChars: a key types one character (two for one beyond the BMP)

_LAYOUT_LIBS: Any = None  # (HIToolbox, CoreServices, CoreFoundation, the two property keys), once loaded


def _declare_tis(lib: Any) -> Any:
    """TextInputSources.h (``TISInputSourceRef`` and ``CFStringRef`` are pointers) and Events.h, in HIToolbox."""
    p = ctypes.c_void_p
    for name, restype, argtypes in (
        # TextInputSources.h: TISInputSourceRef TISCopyCurrentKeyboardLayoutInputSource(void): "the keyboard
        #   layout currently being used" (the one an input method types with, too); the Copy rule: CFRelease it
        ("TISCopyCurrentKeyboardLayoutInputSource", p, []),
        # TextInputSources.h: void *TISGetInputSourceProperty(TISInputSourceRef inputSource, CFStringRef
        #   propertyKey); the Get rule: not released
        ("TISGetInputSourceProperty", p, [p, p]),
        # Events.h: UInt8 LMGetKbdType(void): the physical keyboard type, as UCKeyTranslate wants it
        ("LMGetKbdType", ctypes.c_uint8, []),
    ):  # fmt: skip
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    return lib


def _declare_uc(lib: Any) -> Any:
    """UnicodeUtilities.h, in CarbonCore. MacTypes.h: UInt16, UInt32 and OptionBits as named,
    UniCharCount ``unsigned long``, UniChar a UInt16."""
    u16, u32 = ctypes.c_uint16, ctypes.c_uint32
    # OSStatus UCKeyTranslate(const UCKeyboardLayout *keyLayoutPtr, UInt16 virtualKeyCode, UInt16 keyAction,
    #   UInt32 modifierKeyState, UInt32 keyboardType, OptionBits keyTranslateOptions, UInt32 *deadKeyState,
    #   UniCharCount maxStringLength, UniCharCount *actualStringLength, UniChar unicodeString[])
    fn = lib.UCKeyTranslate
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_void_p, u16, u16, u32, u32, u32, ctypes.POINTER(u32), ctypes.c_ulong,
                   ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(u16)]
    return lib


def _declare_cf(lib: Any) -> Any:
    """CoreFoundation (every CF*Ref is a pointer; CFBase.h: CFIndex a ``signed long``; MacTypes.h: Boolean
    an ``unsigned char``)."""
    p = ctypes.c_void_p
    for name, restype, argtypes in (
        ("CFDataGetBytePtr", p, [p]),  # CFData.h: const UInt8 *CFDataGetBytePtr(CFDataRef theData)
        # CFString.h: Boolean CFStringGetCString(CFStringRef theString, char *buffer, CFIndex bufferSize,
        #   CFStringEncoding encoding) (CFStringEncoding: a UInt32)
        ("CFStringGetCString", ctypes.c_ubyte, [p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
        ("CFRelease", None, [p]),  # CFBase.h: void CFRelease(CFTypeRef cf)
    ):  # fmt: skip
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    return lib


def _layout_libs() -> tuple[Any, Any, Any, int, int]:
    global _LAYOUT_LIBS
    if _LAYOUT_LIBS is None:
        if sys.platform != "darwin":
            raise OSError("the keyboard layout is read from macOS")
        tis = _open(CARBON_PATHS, _declare_tis)
        # TextInputSources.h: extern const CFStringRef kTISPropertyInputSourceID, kTISPropertyUnicodeKeyLayoutData:
        # variables holding the keys, which in_dll reads (a missing one raises ValueError)
        try:
            source_id = ctypes.c_void_p.in_dll(tis, "kTISPropertyInputSourceID").value
            uchr = ctypes.c_void_p.in_dll(tis, "kTISPropertyUnicodeKeyLayoutData").value
        except ValueError as exc:
            raise OSError(str(exc)) from None
        _LAYOUT_LIBS = (tis, _open(CORE_SERVICES_PATHS, _declare_uc), _open(CORE_FOUNDATION_PATHS, _declare_cf),
                        source_id, uchr)
    return _LAYOUT_LIBS


def _cf_string(cf: Any, ref: Any) -> str:
    buf = ctypes.create_string_buffer(256)
    if ref and cf.CFStringGetCString(ref, buf, len(buf), UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def current_layout() -> tuple[str, dict[str, str]]:
    """``(input source ID, {code: character})``: what each letter and punctuation key types with nothing
    held on the keyboard layout in use — ``("com.apple.keylayout.French", {"KeyQ": "a", "KeyA": "q",
    "Semicolon": "m", …})`` — as ``UCKeyTranslate`` gives it from the layout's 'uchr' data. A key that
    types no single visible character is left out.

    Main thread only (TextInputSources.h: "If you are a UI application, you must call TextInputSources
    API on the main thread"; Events.h: LMGetKbdType is not thread safe). Raises ``OSError`` off macOS,
    or for a layout with only 'KCHR' data, whose 'uchr' data is NULL."""
    tis, uc, cf, source_id, uchr = _layout_libs()
    source = tis.TISCopyCurrentKeyboardLayoutInputSource()
    if not source:
        raise OSError("macOS gave no current keyboard layout")
    try:
        ident = _cf_string(cf, tis.TISGetInputSourceProperty(source, source_id))
        data = tis.TISGetInputSourceProperty(source, uchr)  # a CFDataRef, owned by the source: kept until released
        if not data:
            raise OSError(f"the keyboard layout {ident or '?'} has no 'uchr' data")
        layout = cf.CFDataGetBytePtr(data)
        kind = tis.LMGetKbdType()
        utf16 = "utf-16-le" if sys.byteorder == "little" else "utf-16-be"  # UniChars, in the CPU's order
        keys: dict[str, str] = {}
        for code in LAYOUT_CODES:
            dead, length, chars = ctypes.c_uint32(0), ctypes.c_ulong(0), (ctypes.c_uint16 * MAX_CHARS)()
            err = uc.UCKeyTranslate(layout, KEY_CODES[code], KEY_ACTION_DOWN, 0, kind, NO_DEAD_KEYS, ctypes.byref(dead),
                                    MAX_CHARS, ctypes.byref(length), chars)  # modifierKeyState 0: nothing held
            if err != NO_ERR:
                continue
            char = bytes(chars)[: 2 * min(length.value, MAX_CHARS)].decode(utf16, "replace")
            if len(char) == 1 and char.isprintable() and not char.isspace():
                keys[code] = char
        return ident, keys
    finally:
        cf.CFRelease(source)


def layout_problem(source_id: str, keys: Mapping[str, str]) -> str | None:
    """What is wrong with ``keys`` (:func:`current_layout`'s) as a keyboard layout's, or ``None`` — the
    self-test's check: each letter key types a character of its own, with nothing held (no capitals),
    and on a US layout (:data:`US_LAYOUTS`) the one a US keyboard prints on it. The two keys an ISO
    keyboard swaps (Backquote and IntlBackslash) are left out of that."""
    letters = {code: keys.get(code, "") for code in LAYOUT_CODES if code.startswith("Key")}
    missing = [code for code, char in letters.items() if len(char) != 1]
    if missing:
        return "no character for " + ", ".join(missing)
    if len(set(letters.values())) != len(letters):
        return "two letter keys type the same character"
    if any(char != char.lower() for char in letters.values()):
        return "a letter key types a capital with nothing held"
    if source_id in US_LAYOUTS:
        wrong = [f"{code} {keys.get(code)!r}" for code in LAYOUT_CODES
                 if code not in ("Backquote", "IntlBackslash") and keys.get(code) != label(code).lower()]
        if wrong:
            return f"{source_id} is a US layout, but: " + ", ".join(wrong)
    return None
