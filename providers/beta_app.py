import random

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from web3 import Web3

PROVIDER_ADDRESS = Web3.to_checksum_address("0xDaaA2F9b185c1D88D19Fc63d8D4480D5459b9308")
PROVIDER_NAME = "Data Provider Beta"
RANDOM_COUNT = 5
RESOURCE_ID = "random-numbers-5"
DEFAULT_PORT = 8002

app = FastAPI(
    title=PROVIDER_NAME,
    description="Independent data provider service (5 random numbers).",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {
        "provider": PROVIDER_ADDRESS,
        "name": PROVIDER_NAME,
        "resourceId": RESOURCE_ID,
        "count": RANDOM_COUNT,
        "numbers": numbers,
    }
