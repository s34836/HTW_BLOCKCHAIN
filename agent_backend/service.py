"""Micropayment + provider orchestration for agents."""

import logging
from typing import Any

logger = logging.getLogger("agent")

import httpx
from eth_account import Account
from web3 import Web3

from agent_backend.settings import agent_settings

PROVIDER_SERVICE_URLS = [
    agent_settings.provider_alpha_url,
    agent_settings.provider_beta_url,
]


def get_agent_credentials() -> tuple[str, str]:
    key = agent_settings.agent_private_key or agent_settings.owner_private_key
    if not key:
        raise ValueError("Set AGENT_PRIVATE_KEY or OWNER_PRIVATE_KEY in .env")
    address = agent_settings.agent_address or Account.from_key(key).address
    return Web3.to_checksum_address(address), key


def get_oracle_credentials() -> tuple[str, str]:
    key = (
        agent_settings.oracle_private_key
        or agent_settings.agent_private_key
        or agent_settings.owner_private_key
    )
    if not key:
        raise ValueError("Set ORACLE_PRIVATE_KEY, AGENT_PRIVATE_KEY, or OWNER_PRIVATE_KEY in .env")
    address = Account.from_key(key).address
    return Web3.to_checksum_address(address), key


def discover_provider_services() -> list[dict[str, Any]]:
    logger.info("Discovering providers at %s", ", ".join(PROVIDER_SERVICE_URLS))
    catalog: list[dict[str, Any]] = []
    for base_url in PROVIDER_SERVICE_URLS:
        url = base_url.rstrip("/")
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.get(f"{url}/")
                response.raise_for_status()
                info = response.json()
                info["serviceUrl"] = url
                info["online"] = True
                logger.info("Provider online: %s (%s numbers)", info.get("name"), info.get("randomCount"))
                catalog.append(info)
        except httpx.HTTPError as exc:
            logger.warning("Provider offline %s: %s", url, exc)
            catalog.append(
                {
                    "serviceUrl": url,
                    "online": False,
                    "error": str(exc),
                }
            )
    return catalog


def pick_provider_for_count(catalog: list[dict[str, Any]], count: int) -> dict[str, Any]:
    matches = [
        entry
        for entry in catalog
        if entry.get("online") and entry.get("randomCount") == count
    ]
    if not matches:
        raise ValueError(f"No online provider offers exactly {count} random numbers.")
    if len(matches) > 1:
        matches.sort(key=lambda item: item.get("name", ""))
    return matches[0]


def fetch_provider_payload(service_url: str) -> dict[str, Any]:
    url = f"{service_url.rstrip('/')}/random-numbers"
    with httpx.Client(timeout=10.0) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def read_provider_price_wei(contract_instance, address: str) -> int:
    return int(
        contract_instance.functions.providerPriceWei(Web3.to_checksum_address(address)).call()
    )


def enrich_catalog_with_approval(contract_instance, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for entry in catalog:
        item = dict(entry)
        address = item.get("address")
        if address and item.get("online"):
            checksum = Web3.to_checksum_address(address)
            try:
                item["approvedOnChain"] = contract_instance.functions.approvedProviders(checksum).call()
                item["priceWei"] = read_provider_price_wei(contract_instance, checksum)
            except Exception:
                item["approvedOnChain"] = False
                item["priceWei"] = 0
        else:
            item["approvedOnChain"] = False
            item["priceWei"] = 0
        enriched.append(item)
    return enriched


def resolve_request_id(contract_instance, receipt) -> int:
    """Backward-compatible alias for receipt → requestId parsing."""
    from agent_backend.payment_flow import resolve_request_id_from_receipt

    return resolve_request_id_from_receipt(contract_instance, receipt)
