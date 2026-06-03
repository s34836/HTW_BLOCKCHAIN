import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")


class AgentSettings:
    def __init__(self) -> None:
        self.supervisor_url = os.getenv("SUPERVISOR_URL", "http://127.0.0.1:8000")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.http_timeout = float(os.getenv("AGENT_HTTP_TIMEOUT", "180"))
        self.agent_address = os.getenv(
            "AGENT_ADDRESS",
            "0x71FE831B3ef3a61e0EAFed83A3da31d2f08D4079",
        )
        self.agent_private_key = os.getenv("AGENT_PRIVATE_KEY", "")
        self.oracle_private_key = os.getenv("ORACLE_PRIVATE_KEY", "")
        self.owner_private_key = os.getenv("OWNER_PRIVATE_KEY", "")
        self.provider_alpha_url = os.getenv("PROVIDER_ALPHA_URL", "http://127.0.0.1:8001")
        self.provider_beta_url = os.getenv("PROVIDER_BETA_URL", "http://127.0.0.1:8002")
        self.agent_payment_wei = int(os.getenv("AGENT_PAYMENT_WEI", "1000"))


agent_settings = AgentSettings()
