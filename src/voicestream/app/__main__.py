"""``python -m voicestream.app``, and the entry point of the packaged Voicestream.app.

PyInstaller runs this file as a plain script (no parent package), hence the absolute import.
"""

import multiprocessing
import sys

from voicestream.app.launcher import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # a frozen app re-executes itself for any spawned child process
    sys.exit(main())
