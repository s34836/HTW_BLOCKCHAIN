import random

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from web3 import Web3

from htw_logging import attach_request_logging

PROVIDER_ADDRESS = Web3.to_checksum_address("0xb91A1B6Fb3d910710984c301Cb162460Aef3b209")
PROVIDER_NAME = "Data Provider Alpha"
RANDOM_COUNT = 10
RESOURCE_ID = "random-numbers-10"
DEFAULT_PORT = 8001

app = FastAPI(
    title=PROVIDER_NAME,
    description="Independent data provider service (10 random numbers).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger = attach_request_logging(app, "provider-alpha")


@app.on_event("startup")
def log_startup():
    logger.info(
        "Provider Alpha ready | address=%s | port=%s | resource=%s",
        PROVIDER_ADDRESS,
        DEFAULT_PORT,
        RESOURCE_ID,
    )


@app.get("/")
def provider_info():
    return {
        "name": PROVIDER_NAME,
        "address": PROVIDER_ADDRESS,
        "resourceId": RESOURCE_ID,
        "randomCount": RANDOM_COUNT,
        "endpoints": ["/health", "/random-numbers"],
    }


@app.get("/health")
def health():
    return {"status": "ok", "provider": PROVIDER_ADDRESS}


@app.get("/random-numbers")
def random_numbers():
    numbers = [random.randint(1, 1000) for _ in range(RANDOM_COUNT)]
    logger.info("Serving %s random numbers to client", RANDOM_COUNT)
    return {
        "provider": PROVIDER_ADDRESS,
        "name": PROVIDER_NAME,
        "resourceId": RESOURCE_ID,
        "count": RANDOM_COUNT,
        "numbers": numbers,
    }
