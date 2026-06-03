"""On-chain request lifecycle: confirm delivery and release payment to provider."""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

from eth_account import Account
from fastapi import HTTPException
from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

from agent_system.service import get_oracle_credentials


class PaymentFlowError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def read_request_state(contract_instance, request_id: int) -> dict[str, Any]:
    result = contract_instance.functions.requests(request_id).call()
    return {
        "requestId": request_id,
        "requester": result[0],
        "provider": result[1],
        "amountWei": int(result[2]),
        "resourceId": result[3],
        "fulfilled": bool(result[4]),
        "paid": bool(result[5]),
    }


def resolve_request_id_from_receipt(contract_instance, receipt) -> int:
    event = contract_instance.events.RequestCreated
    for log_entry in receipt.logs:
        try:
            decoded = event.process_log(log_entry)
            return int(decoded["args"]["requestId"])
        except Exception:
            continue
    return int(contract_instance.functions.requestCount().call())


def assert_oracle_matches_contract(contract_instance, oracle_address: str) -> None:
    on_chain_oracle = Web3.to_checksum_address(contract_instance.functions.oracle().call())
    signer = Web3.to_checksum_address(oracle_address)
    if signer != on_chain_oracle:
        raise PaymentFlowError(
            f"Oracle signer {signer} does not match contract oracle {on_chain_oracle}. "
            "Set ORACLE_PRIVATE_KEY in .env to the oracle wallet.",
            status_code=400,
        )


def complete_request_payment(
    supervisor_module: Any,
    request_id: int,
    *,
    agent_address: Optional[str] = None,
    agent_private_key: Optional[str] = None,
    oracle_private_key: Optional[str] = None,
) -> dict[str, Any]:
    """Idempotently confirm (if needed) and release payment for a request."""
    contract = supervisor_module.get_contract()
    state = read_request_state(contract, request_id)
    if state["amountWei"] <= 0:
        raise PaymentFlowError(f"Request #{request_id} does not exist on chain.")
    if state["fulfilled"] and state["paid"]:
        provider_balance = supervisor_module.w3.eth.get_balance(state["provider"])
        return {
            "requestId": request_id,
            "provider": state["provider"],
            "amountWei": state["amountWei"],
            "fulfilled": True,
            "paid": True,
            "providerBalanceWei": provider_balance,
            "transactions": {},
            "message": f"Request #{request_id} is already paid on chain.",
        }

    oracle_addr, oracle_key = get_oracle_credentials()
    if oracle_private_key:
        oracle_key = oracle_private_key
        oracle_addr = Web3.to_checksum_address(Account.from_key(oracle_key).address)
    assert_oracle_matches_contract(contract, oracle_addr)

    release_addr, release_key = supervisor_module.get_sender_and_key(
        agent_address, agent_private_key or supervisor_module.OWNER_PRIVATE_KEY
    )
    if agent_private_key:
        release_key = agent_private_key
        release_addr = Web3.to_checksum_address(
            agent_address or Account.from_key(release_key).address
        )

    transactions: dict[str, str] = {}
    try:
        if not state["fulfilled"]:
            logger.info("confirmDelivery request #%s", request_id)
            confirm_receipt = supervisor_module.send_transaction(
                contract.functions.confirmDelivery(request_id),
                oracle_addr,
                oracle_key,
            )
            transactions["confirmDelivery"] = confirm_receipt.transactionHash.hex()
            state = read_request_state(contract, request_id)
            if not state["fulfilled"]:
                raise PaymentFlowError(
                    f"confirmDelivery mined but request #{request_id} is still not fulfilled."
                )

        if not state["paid"]:
            if not contract.functions.approvedProviders(state["provider"]).call():
                raise PaymentFlowError(
                    f"Provider {state['provider']} is not approved; cannot release payment."
                )
            balance = supervisor_module.w3.eth.get_balance(contract.address)
            if balance < state["amountWei"]:
                raise PaymentFlowError(
                    f"Contract balance {balance} wei is less than request amount "
                    f"{state['amountWei']} wei for request #{request_id}."
                )
            logger.info("releasePayment request #%s -> %s", request_id, state["provider"])
            release_receipt = supervisor_module.send_transaction(
                contract.functions.releasePayment(request_id),
                release_addr,
                release_key,
            )
            transactions["releasePayment"] = release_receipt.transactionHash.hex()
            state = read_request_state(contract, request_id)
            if not state["paid"]:
                raise PaymentFlowError(
                    f"releasePayment mined but request #{request_id} is still not marked paid."
                )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        raise PaymentFlowError(detail, status_code=exc.status_code) from exc
    except ContractLogicError as exc:
        raise PaymentFlowError(str(exc)) from exc
    except TimeExhausted as exc:
        raise PaymentFlowError(
            f"Transaction timed out for request #{request_id}. Retry complete-request.",
            status_code=504,
        ) from exc
    except Web3RPCError as exc:
        raise PaymentFlowError(f"RPC error: {exc}", status_code=502) from exc

    provider_balance_after = supervisor_module.w3.eth.get_balance(state["provider"])
    return {
        "requestId": request_id,
        "provider": state["provider"],
        "amountWei": state["amountWei"],
        "fulfilled": state["fulfilled"],
        "paid": state["paid"],
        "providerBalanceWei": provider_balance_after,
        "transactions": transactions,
        "message": f"Request #{request_id}: {state['amountWei']} wei released to provider.",
    }


def refund_request_to_buyer(supervisor_module: Any, request_id: int) -> dict[str, Any]:
    """Return escrowed wei to the requester (owner-only on chain)."""
    contract = supervisor_module.get_contract()
    state = read_request_state(contract, request_id)
    if state["amountWei"] <= 0:
        raise PaymentFlowError(f"Request #{request_id} does not exist on chain.")
    if state["paid"]:
        if state["fulfilled"]:
            raise PaymentFlowError(f"Request #{request_id} was already paid to the provider.")
        return {
            "requestId": request_id,
            "requester": state["requester"],
            "amountWei": state["amountWei"],
            "fulfilled": state["fulfilled"],
            "paid": True,
            "transactions": {},
            "message": f"Request #{request_id} was already returned to the buyer.",
        }

    balance = supervisor_module.w3.eth.get_balance(contract.address)
    if balance < state["amountWei"]:
        raise PaymentFlowError(
            f"Contract balance {balance} wei is less than request amount "
            f"{state['amountWei']} wei for request #{request_id}."
        )

    owner_addr, owner_key = supervisor_module.get_sender_and_key(None, None)
    transactions: dict[str, str] = {}
    try:
        logger.info("refundToRequester request #%s -> %s", request_id, state["requester"])
        receipt = supervisor_module.send_transaction(
            contract.functions.refundToRequester(request_id),
            owner_addr,
            owner_key,
        )
        transactions["refundToRequester"] = receipt.transactionHash.hex()
        state = read_request_state(contract, request_id)
        if not state["paid"]:
            raise PaymentFlowError(
                f"refundToRequester mined but request #{request_id} is still not settled."
            )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        raise PaymentFlowError(detail, status_code=exc.status_code) from exc
    except ContractLogicError as exc:
        raise PaymentFlowError(str(exc)) from exc
    except TimeExhausted as exc:
        raise PaymentFlowError(
            f"Transaction timed out for request #{request_id}. Retry refund.",
            status_code=504,
        ) from exc
    except Web3RPCError as exc:
        raise PaymentFlowError(f"RPC error: {exc}", status_code=502) from exc

    return {
        "requestId": request_id,
        "requester": state["requester"],
        "amountWei": state["amountWei"],
        "fulfilled": state["fulfilled"],
        "paid": state["paid"],
        "transactions": transactions,
        "message": f"Request #{request_id}: {state['amountWei']} wei returned to buyer.",
    }


def list_pending_requests(contract_instance) -> list[dict[str, Any]]:
    count = int(contract_instance.functions.requestCount().call())
    pending = []
    for request_id in range(1, count + 1):
        state = read_request_state(contract_instance, request_id)
        if state["amountWei"] <= 0:
            continue
        if not state["paid"]:
            pending.append(state)
    return pending
