import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
DOTENV_PATH = ROOT_DIR / ".env"
load_dotenv(dotenv_path=DOTENV_PATH)


class Settings:
    def __init__(self) -> None:
        self.web3_provider_uri = os.getenv("WEB3_PROVIDER_URI", "")
        self.chain_id = int(os.getenv("CHAIN_ID", "11155111"))
        self.contract_address = os.getenv("CONTRACT_ADDRESS", "")
        self.default_from_address = os.getenv("DEFAULT_FROM_ADDRESS", "")
        self.owner_private_key = os.getenv("OWNER_PRIVATE_KEY", "")


settings = Settings()
