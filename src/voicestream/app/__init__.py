"""The Voicestream app: a long-lived host around the assistant, for the Mac window and menu bar.

The command line runs one :class:`~voicestream.session.Assistant` per process and talks to you
in the terminal. The app instead keeps one :class:`~voicestream.app.host.AppHost` alive for as
long as it is open: it owns the dashboard server, the event bus and the cost meter, and starts,
pauses, stops and restarts assistants as you click. Settings, keys and app preferences are
written from the UI (:mod:`.settings`, :mod:`.secrets`, :mod:`.prefs`) instead of by hand.

Nothing heavy is imported here: :mod:`voicestream.config` reads keys through
:mod:`.secrets` on every start, the CLI included.
"""
