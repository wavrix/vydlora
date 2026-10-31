#!/usr/bin/env python3
"""Start the bundled Lora 1 engine with project-local, private runtime data."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from app.core.compat import apply_environment_aliases, upgrade_profile_database

apply_environment_aliases()
DB_FILENAME = "lora1.db"


def _force_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _dispatch_module_run() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "-m":
        import runpy
        module, sys.argv = sys.argv[2], sys.argv[3:]
        sys.argv.insert(0, module)
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        raise SystemExit(0)


def _data_dir() -> Path:
    from app.core.profiles import resolve_profile
    root = Path(os.environ.get("LORA1_DATA_DIR") or PROJECT_ROOT / ".runtime" / "lora1").resolve()
    os.environ["LORA1_PROFILES_ROOT"] = str(root)
    base = resolve_profile(root)
    for sub in ("", "logs", "cache", "cache/downloads", "tmp", "home"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    upgrade_profile_database(base)
    os.environ["LORA1_DATA_DIR"] = str(base)
    return base


def _default_downloads_dir() -> Path:
    data = Path(os.environ.get("LORA1_DATA_DIR") or PROJECT_ROOT / ".runtime" / "lora1")
    downloads = data / "cache" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    return downloads


def _configure_env(data: Path) -> None:
    os.environ.setdefault("ENVIRONMENT", "development")
    os.environ.setdefault("LOG_LEVEL", "INFO")
    paths = {
        "DATABASE_URL": "sqlite:///" + (data / DB_FILENAME).as_posix(),
        "DOWNLOADS_DIR": str(_default_downloads_dir()),
        "COOKIES_FILE": str(data / "cookies.txt"),
    }
    root = Path(os.environ.get("LORA1_PROFILES_ROOT", str(data)))
    for name, value in paths.items():
        if (root / "active-profile.json").is_file():
            os.environ[name] = value
        else:
            os.environ.setdefault(name, value)
    _configure_playwright()


def _configure_playwright() -> None:
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    local = os.environ.get("LOCALAPPDATA")
    cache = Path(local) / "ms-playwright" if local else Path.home() / ".cache" / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)


def _add_file_logging(data: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(
        data / "logs" / "engine.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
    logging.getLogger().addHandler(handler)


def main() -> None:
    _force_utf8_streams()
    _dispatch_module_run()
    data = _data_dir()
    _configure_env(data)
    import uvicorn
    from app.main import app
    from app.core.engine_security import wrap

    _add_file_logging(data)
    token_file = os.environ.get("LORA1_AUTH_TOKEN_FILE")
    token = Path(token_file).read_text().strip() if token_file else os.environ.get("LORA1_AUTH_TOKEN", "")
    holder = {"server": None}
    guarded = wrap(app, token=token, on_shutdown=lambda: _request_stop(holder))
    server = uvicorn.Server(uvicorn.Config(
        guarded,
        host=os.environ.get("LORA1_HOST", "127.0.0.1"),
        port=int(os.environ.get("LORA1_PORT", "8000")),
        log_config=None,
        access_log=False,
        lifespan="on",
        workers=1,
    ))
    holder["server"] = server
    logging.getLogger("engine").info("Lora 1 engine ready at http://%s:%s", server.config.host, server.config.port)
    server.run()


def _request_stop(holder: dict) -> None:
    if holder.get("server") is not None:
        holder["server"].should_exit = True


if __name__ == "__main__":
    main()
