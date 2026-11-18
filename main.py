"""Entry point: `python main.py`, then open http://localhost:8080.

Startup order:
  1. read settings from .env
  2. set up API-call logging
  3. create ONE API client shared by every browser tab
  4. register the "/" page (built fresh for each tab that opens it)
  5. start the web server
"""
import logging
import os

from dotenv import load_dotenv
from nicegui import app, ui

from app.api_client import ConfigServiceClient
from app.ui import build_page

# ---- 1. settings -------------------------------------------------------------
# load_dotenv() copies the values in .env into environment variables.
# It does NOT override a variable already set in the shell, so
# `PORT=9000 python main.py` still wins over a PORT in .env.
# .env is gitignored, so the API key never ends up in the repository.
load_dotenv()
BASE_URL = os.getenv("TM_API_BASE_URL", "")
API_KEY = os.getenv("TM_API_KEY", "")   # default "" so a missing value is handled below, not a crash

# ---- 2. API-call logging -----------------------------------------------------
# The browser never calls the Configuration Service (the server does), so DevTools
# can't show those calls. Instead they are printed in this terminal:
#   failures are always printed;
#   LOG_API=1 in .env also prints every successful call.
_handler = logging.StreamHandler()                                   # print to the terminal (stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s  API  %(message)s", "%H:%M:%S"))
_api_log = logging.getLogger("temporal.api")                         # same name api_client.py logs to
_api_log.addHandler(_handler)
# propagate=False: don't also pass messages up to parent loggers. The web server
# (uvicorn) configures logging too, and without this a line could print twice.
_api_log.propagate = False
_api_log.setLevel(logging.INFO if os.getenv("LOG_API", "").lower() in {"1", "true", "yes"}
                  else logging.WARNING)

# ---- 3. one shared API client --------------------------------------------------
# One client for the whole server process: it reuses HTTP connections, and its part
# cache is shared by every browser tab. None if settings are missing (see index()).
api = ConfigServiceClient(BASE_URL, API_KEY) if BASE_URL and API_KEY else None
if api:
    # Close the HTTP connections cleanly when the server stops.
    app.on_shutdown(api.close)


# ---- 4. the page ---------------------------------------------------------------
# @ui.page("/") registers index() as the handler for the URL "/".
# It runs once PER BROWSER TAB that opens the page (not once at startup), so each
# tab gets its own fresh UI and its own state.
@ui.page("/")
async def index() -> None:
    # If settings are missing, show a friendly message instead of crashing.
    if api is None:
        with ui.card().classes("q-ma-xl"):
            ui.label("Missing configuration").classes("text-h6")
            ui.markdown("Copy `.env.example` to `.env` and set `TM_API_BASE_URL` and `TM_API_KEY`, "
                        "then restart.")
        return
    # Build the real interface (header, tree, details panel). See app/ui.py.
    await build_page(api)


# ---- 5. start the server -------------------------------------------------------
# NiceGUI auto-reloads when a .py file is saved. The reloader runs the app in a child
# process, where this file is loaded as "__mp_main__" instead of "__main__".
# Checking both names makes ui.run work in both processes.
# (Changes to .env are NOT watched: press Ctrl+C and start again.)
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="Temporal Control Interface", port=int(os.getenv("PORT", "8080")),
           favicon="⏳", show=False)   # show=False: don't open a browser window automatically
