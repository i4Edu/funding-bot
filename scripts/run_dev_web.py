from __future__ import annotations

import importlib.util
import os

from werkzeug.serving import run_simple

from web.app import app


def _watchdog_available() -> bool:
    return importlib.util.find_spec("watchdog.observers") is not None


def main() -> None:
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug_enabled = os.environ.get("FLASK_DEBUG", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    run_simple(
        hostname=host,
        port=port,
        application=app,
        use_reloader=True,
        use_debugger=debug_enabled,
        threaded=True,
        reloader_type="watchdog" if _watchdog_available() else "stat",
    )


if __name__ == "__main__":
    main()
