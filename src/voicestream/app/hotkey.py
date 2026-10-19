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

Every constant and signature below comes from the macOS SDK headers, quoted by name: HIToolbox's
``CarbonEvents.h``, ``CarbonEventsCore.h`` and ``Events.h``
(``Carbon.framework/Frameworks/HIToolbox.framework/Headers``), ``MacTypes.h`` for the base types,
and AppKit's ``NSEvent.h`` and ``NSText.h`` for the menu.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)

MODIFIERS = ("ctrl", "alt", "shift", "cmd")
"""In the order macOS writes them: ⌃⌥⇧⌘."""
GLYPHS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
DEFAULT_CODE, DEFAULT_MODS = "KeyR", ("alt", "cmd")  # ⌥⌘R: R for record

TOGGLE = {"stopped": "start", "error": "restart", "listening": "stop", "paused": "resume"}
"""What a press does in each state (:meth:`voicestream.app.host.AppHost.listen_toggle`); in the
others (starting, stopping) it does nothing."""

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
        "cmd+F5", "alt+cmd+F5", "ctrl+F2", "ctrl+F3",                  # VoiceOver, accessibility, keyboard focus
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
        return key.upper()
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


def carbon() -> Any:
    """HIToolbox with the hot key calls declared (loaded once). Raises ``OSError`` off macOS, or where
    it can't be loaded."""
    global _LIB
    if _LIB is None:
        if sys.platform != "darwin":
            raise OSError("global keyboard shortcuts are a macOS feature")
        errors = []
        for path in CARBON_PATHS:
            try:
                _LIB = _declare(ctypes.CDLL(path))
                break
            except (OSError, AttributeError) as exc:  # AttributeError: a symbol is missing
                errors.append(f"{path}: {exc}")
        else:
            raise OSError("; ".join(errors))
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
