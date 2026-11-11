"""Application logging.

The packaged Volgas.exe has no console, so before this module existed any
error raised in a background thread or a Tk callback simply vanished. Now
everything goes to a rotating log file in the user's data folder (see
volgas_settings.data_dir), which a user can attach to a bug report.

Usage: call setup_logging() once at startup, then in any module
    log = logging.getLogger(__name__)
"""
import logging
import logging.handlers
import os
import sys
import threading

LOG_FILE_NAME = "volgas.log"
LOG_MAX_BYTES = 1_000_000   # ~1 MB per file
LOG_BACKUPS = 3             # volgas.log + volgas.log.1..3

_configured = False


def setup_logging(log_dir, level=logging.INFO):
    """Send log records to <log_dir>/volgas.log (rotating) and, when a
    console exists, to stderr. Also routes uncaught exceptions from the
    main thread and worker threads into the log. Safe to call twice.
    Returns the log file path, or None if the folder couldn't be used."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return getattr(root, "_volgas_log_path", None)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s")

    path = None
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, LOG_FILE_NAME)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        path = None  # read-only or missing folder - still log to the console below

    if sys.stderr is not None:  # None in a windowed PyInstaller exe
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    def excepthook(exc_type, exc, tb):
        logging.getLogger("volgas").critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        logging.getLogger("volgas").error(
            "uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    root._volgas_log_path = path
    _configured = True
    return path
