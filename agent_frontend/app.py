import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from htw_logging import attach_request_logging

ROOT_DIR = Path(__file__).resolve().parent
REPO_ROOT = ROOT_DIR.parent
load_dotenv(dotenv_path=REPO_ROOT / ".env")

SUPERVISOR_API_URL = os.getenv("SUPERVISOR_API_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_PORT = int(os.getenv("AGENT_CHAT_PORT", "8003"))

app = FastAPI(
    title="Agent Chat UI",
    description="Standalone frontend for the LLM agent chat (API on supervisor).",
    version="1.0.0",
)

logger = attach_request_logging(app, "agent-ui")
app.mount("/static", StaticFiles(directory=ROOT_DIR), name="static")


@app.on_event("startup")
def log_startup():
    logger.info(
        "Agent chat UI ready | port=%s | supervisor API=%s",
        DEFAULT_PORT,
        SUPERVISOR_API_URL,
    )


@app.get("/config.js")
def serve_config():
    logger.debug("Serving config.js")
    return Response(
        content=f'window.SUPERVISOR_API_URL = {repr(SUPERVISOR_API_URL)};',
        media_type="application/javascript",
    )


@app.get("/")
def serve_chat():
    logger.info("Serving chat page")
    return FileResponse(ROOT_DIR / "index.html")
