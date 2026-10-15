"""Live local web dashboard (``pip install 'voicestream[dashboard]'``).

Starlette and uvicorn are an optional extra, so nothing is imported here: the CLI
imports :mod:`voicestream.dashboard.server` lazily and falls back to the terminal
when it isn't installed.
"""
