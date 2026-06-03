import json
from typing import Any

import httpx
from langchain_core.tools import tool

from agent_system.purchase_flow import PurchaseError, run_purchase_random_numbers
from agent_system.settings import agent_settings


def _supervisor_request(method: str, path: str, json_body: dict | None = None) -> dict[str, Any]:
    url = f"{agent_settings.supervisor_url.rstrip('/')}{path}"
    with httpx.Client(timeout=agent_settings.http_timeout) as client:
        if method == "GET":
            response = client.get(url)
        else:
            response = client.post(url, json=json_body)
        response.raise_for_status()
        return response.json()


@tool
def list_available_providers() -> str:
    """Ask the supervisor backend which data providers exist, what they offer, and if they are approved on Sepolia."""
    payload = _supervisor_request("GET", "/agent/provider-catalog")
    return json.dumps(payload, indent=2)


@tool
def purchase_random_numbers(count: int) -> str:
    """Buy random numbers from an approved provider via on-chain micropayment.

    Use when the user needs random data from providers (do not make up numbers).
    count must be 5 or 10: use 10 for larger samples (~10 numbers), 5 for smaller (~5 numbers).
    Payment wei is taken from the on-chain price configured per provider in the supervisor contract.
    """
    if count not in (5, 10):
        return json.dumps({"error": "count must be 5 or 10"})
    try:
        payload = run_purchase_random_numbers(count=count)
        return json.dumps(payload, indent=2)
    except PurchaseError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
