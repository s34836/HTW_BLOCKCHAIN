"""In-process micropayment purchase (used by API and LangChain tools)."""

import logging
from typing import Any, Optional

logger = logging.getLogger("agent")

from eth_account import Account
from fastapi import HTTPException
from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

from agent_backend.payment_flow import (
    PaymentFlowError,
    complete_request_payment,
    resolve_request_id_from_receipt,
)
from agent_backend.service import (
    discover_provider_services,
    enrich_catalog_with_approval,
    fetch_provider_payload,
    get_agent_credentials,
    get_oracle_credentials,
    pick_provider_for_count,
    read_provider_price_wei,
)


class PurchaseError(Exception):
    """Raised when purchase cannot complete; message is safe to show to the user."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _map_flow_error(exc: PaymentFlowError) -> PurchaseError:
    return PurchaseError(str(exc), status_code=exc.status_code)


def run_purchase_random_numbers(
    count: int,
    amount_wei: Optional[int] = None,
    agent_address: Optional[str] = None,
    agent_private_key: Optional[str] = None,
    oracle_private_key: Optional[str] = None,
) -> dict[str, Any]:
    if count not in (5, 10):
        raise PurchaseError("count must be 5 or 10")

    import backend_supervisor.app as supervisor

    logger.info("Purchase: count=%s", count)
    contract = supervisor.get_contract()
    catalog = enrich_catalog_with_approval(contract, discover_provider_services())
    try:
        provider = pick_provider_for_count(catalog, count)
    except ValueError as exc:
        raise PurchaseError(str(exc)) from exc

    if not provider.get("approvedOnChain"):
        raise PurchaseError(
            f"Provider {provider.get('address')} is not approved on chain. "
            "Approve it in the Supervisor dashboard first."
        )

    agent_addr, agent_key = get_agent_credentials()
    if agent_private_key:
        agent_key = agent_private_key
        agent_addr = Web3.to_checksum_address(
            agent_address or Account.from_key(agent_key).address
        )
    if oracle_private_key:
        get_oracle_credentials()  # validate key exists

    provider_address = Web3.to_checksum_address(provider["address"])
    logger.info(
        "Selected provider %s (%s wei) resource=%s",
        provider_address,
        provider.get("priceWei"),
        provider.get("resourceId"),
    )
    resource_id = provider["resourceId"]
    payment_wei = amount_wei or int(provider.get("priceWei") or 0)
    if payment_wei <= 0:
        payment_wei = read_provider_price_wei(contract, provider_address)
    if payment_wei <= 0:
        raise PurchaseError(
            f"On-chain price for provider {provider_address} is not set. "
            "Set it in the Supervisor dashboard."
        )

    transactions: dict[str, str] = {}
    try:
        request_receipt = supervisor.send_transaction(
            contract.functions.requestResource(provider_address, resource_id),
            agent_addr,
            agent_key,
            value=payment_wei,
        )
        transactions["requestResource"] = request_receipt.transactionHash.hex()
        request_id = resolve_request_id_from_receipt(contract, request_receipt)
        logger.info("requestResource mined: requestId=%s tx=%s", request_id, transactions["requestResource"])

        data_payload = fetch_provider_payload(provider["serviceUrl"])
        logger.info("Fetched %s numbers from %s", len(data_payload.get("numbers", [])), provider["serviceUrl"])

        completion = complete_request_payment(
            supervisor,
            request_id,
            agent_address=agent_addr,
            agent_private_key=agent_key,
            oracle_private_key=oracle_private_key,
        )
        transactions.update(completion.get("transactions", {}))
    except PaymentFlowError as exc:
        raise _map_flow_error(exc) from exc
    except HTTPException as exc:
        raise PurchaseError(str(exc.detail), status_code=exc.status_code) from exc
    except ContractLogicError as exc:
        raise PurchaseError(str(exc)) from exc
    except TimeExhausted as exc:
        raise PurchaseError(
            "Transaction not confirmed in time (Sepolia may be slow). "
            "Use POST /agent/complete-pending-payments to finish stuck requests.",
            status_code=504,
        ) from exc
    except Web3RPCError as exc:
        message = str(exc)
        if "insufficient funds" in message.lower():
            raise PurchaseError(
                "Agent wallet has insufficient Sepolia ETH for payment and gas.",
                status_code=400,
            ) from exc
        if "replacement transaction underpriced" in message.lower():
            raise PurchaseError(
                "Pending transaction conflict. Wait a minute and try again.",
                status_code=409,
            ) from exc
        raise PurchaseError(f"Blockchain RPC error: {message}", status_code=502) from exc
    except ValueError as exc:
        raise PurchaseError(str(exc)) from exc

    if not completion.get("paid"):
        raise PurchaseError(
            f"Payment for request #{request_id} was not released to the provider."
        )

    return {
        "agent": agent_addr,
        "provider": provider_address,
        "providerName": provider.get("name"),
        "resourceId": resource_id,
        "amountWei": payment_wei,
        "requestId": request_id,
        "numbers": data_payload.get("numbers", []),
        "paid": True,
        "transactions": transactions,
        "paymentMessage": completion.get("message"),
    }
