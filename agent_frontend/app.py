import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

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

app.mount("/static", StaticFiles(directory=ROOT_DIR), name="static")


@app.get("/config.js")
def serve_config():
    return Response(
        content=f'window.SUPERVISOR_API_URL = {repr(SUPERVISOR_API_URL)};',
        media_type="application/javascript",
    )


@app.get("/")
def serve_chat():
    return FileResponse(ROOT_DIR / "index.html")
