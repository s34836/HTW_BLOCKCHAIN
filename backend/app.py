import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import solcx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from agent_system import agent_router
from backend.config import settings

ROOT_DIR = Path(__file__).resolve().parent
CONTRACT_PATH = ROOT_DIR / "contract.sol"
WEB3_PROVIDER_URI = settings.web3_provider_uri
CHAIN_ID = settings.chain_id
CONTRACT_ADDRESS = settings.contract_address
DEFAULT_FROM_ADDRESS = settings.default_from_address
OWNER_PRIVATE_KEY = settings.owner_private_key

app = FastAPI(
    title="AIAgentMicropayment Backend",
    description="Python backend API for interacting with the AIAgentMicropayment Solidity contract.",
    version="1.0.0",
)

frontend_dir = ROOT_DIR / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")
app.include_router(agent_router)

_agent_chat_origin = os.getenv("AGENT_CHAT_ORIGIN", "http://127.0.0.1:8003")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_agent_chat_origin, "http://localhost:8003"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

w3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER_URI))

PROVIDER_APPROVED_TOPIC = Web3.keccak(text="ProviderApproved(address,bool)")
LOG_CHUNK_SIZE = 9_000
CREATION_BLOCK_CACHE: dict[str, int] = {}
APPROVED_PROVIDERS_CACHE: dict[str, list[str]] = {}
logger = logging.getLogger(__name__)
TX_RECEIPT_TIMEOUT_SEC = 120


def ensure_web3_connected():
    last_error = None
    for attempt in range(3):
        try:
            w3.eth.block_number
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    raise HTTPException(
        status_code=503,
        detail=f"Unable to reach Web3 provider at {WEB3_PROVIDER_URI}: {last_error}",
    )


@app.get("/")
def serve_dashboard():
    return FileResponse(frontend_dir / "index.html")


def compile_contract():
    source = CONTRACT_PATH.read_text()
    solcx.install_solc("0.8.20")
    compiled = solcx.compile_standard(
        {
            "language": "Solidity",
            "sources": {"contract.sol": {"content": source}},
            "settings": {
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}}
            },
        },
        solc_version="0.8.20",
    )
    contract_data = compiled["contracts"]["contract.sol"]["AIAgentMicropayment"]
    return contract_data["abi"], contract_data["evm"]["bytecode"]["object"]


abi, bytecode = compile_contract()
contract = None
if CONTRACT_ADDRESS:
    contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=abi)


def get_contract():
    ensure_web3_connected()
    if contract is None:
        raise HTTPException(status_code=400, detail="Contract address is not set. Deploy first or provide CONTRACT_ADDRESS.")
    return contract


def get_contract_creation_block(contract_address: str) -> int:
    checksum_address = Web3.to_checksum_address(contract_address)
    if checksum_address in CREATION_BLOCK_CACHE:
        return CREATION_BLOCK_CACHE[checksum_address]

    latest_block = w3.eth.block_number
    low, high = 0, latest_block
    while low < high:
        mid = (low + high) // 2
        code = w3.eth.get_code(checksum_address, block_identifier=mid)
        if code not in (b"", b"\x00") and len(code) > 0:
            high = mid
        else:
            low = mid + 1

    CREATION_BLOCK_CACHE[checksum_address] = low
    return low


def invalidate_approved_providers_cache(contract_address: Optional[str] = None) -> None:
    if contract_address is None:
        APPROVED_PROVIDERS_CACHE.clear()
        return
    APPROVED_PROVIDERS_CACHE.pop(Web3.to_checksum_address(contract_address), None)


def get_approved_provider_addresses(contract_instance, use_cache: bool = True):
    """Rebuild approved provider list from on-chain ProviderApproved events."""
    ensure_web3_connected()
    cache_key = contract_instance.address
    if use_cache and cache_key in APPROVED_PROVIDERS_CACHE:
        return APPROVED_PROVIDERS_CACHE[cache_key]

    approved_state: dict[str, bool] = {}
    event = contract_instance.events.ProviderApproved
    from_block = get_contract_creation_block(contract_instance.address)
    latest_block = w3.eth.block_number

    while from_block <= latest_block:
        to_block = min(from_block + LOG_CHUNK_SIZE - 1, latest_block)
        try:
            logs = w3.eth.get_logs(
                {
                    "address": contract_instance.address,
                    "topics": [PROVIDER_APPROVED_TOPIC],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            )
            for log in logs:
                decoded = event.process_log(log)
                provider = Web3.to_checksum_address(decoded["args"]["provider"])
                approved_state[provider] = bool(decoded["args"]["approved"])
        except Exception:
            pass
        from_block = to_block + 1

    verified = []
    for provider, is_approved in approved_state.items():
        if not is_approved:
            continue
        try:
            if contract_instance.functions.approvedProviders(provider).call():
                verified.append(provider)
        except Exception:
            continue
    verified = sorted(verified)
    APPROVED_PROVIDERS_CACHE[cache_key] = verified
    return verified


def _raw_transaction_bytes(signed_transaction):
    raw_tx = getattr(signed_transaction, "raw_transaction", None)
    if raw_tx is None:
        raw_tx = signed_transaction.rawTransaction
    return raw_tx


def get_sender_and_key(from_address: Optional[str], private_key: Optional[str]):
    ensure_web3_connected()
    sender = from_address or DEFAULT_FROM_ADDRESS
    key = private_key or OWNER_PRIVATE_KEY
    if sender:
        sender = Web3.to_checksum_address(sender)
    elif key:
        account = w3.eth.account.from_key(key)
        sender = account.address
    else:
        accounts = w3.eth.accounts
        if not accounts:
            raise HTTPException(
                status_code=400,
                detail="No signing key available. Set OWNER_PRIVATE_KEY in .env (must match contract owner).",
            )
        sender = accounts[0]
    if not key and not w3.eth.accounts:
        raise HTTPException(
            status_code=400,
            detail="OWNER_PRIVATE_KEY is required to sign transactions on Sepolia.",
        )
    return sender, key


def send_transaction(function, from_address: Optional[str] = None, private_key: Optional[str] = None, value: int = 0):
    ensure_web3_connected()
    sender, key = get_sender_and_key(from_address, private_key)
    tx_params = {
        "from": sender,
        "value": value,
        "gas": 500000,
        "chainId": CHAIN_ID,
    }
    if key:
        nonce = w3.eth.get_transaction_count(sender, "pending")
        tx = function.build_transaction({
            **tx_params,
            "nonce": nonce,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, key)
        tx_hash = w3.eth.send_raw_transaction(_raw_transaction_bytes(signed))
    else:
        tx_hash = function.transact(tx_params)
    receipt = _wait_for_receipt(tx_hash)
    if receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction reverted on chain. Check owner wallet, gas, and contract state.")
    return receipt


def _wait_for_receipt(tx_hash, timeout: int = TX_RECEIPT_TIMEOUT_SEC):
    """Wait for mining; return 504 with tx hash if Sepolia is slow."""
    from web3.exceptions import TimeExhausted

    tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    try:
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout, poll_latency=2)
    except TimeExhausted as exc:
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Transaction {tx_hex} not confirmed within {timeout}s. "
                    "Check Sepolia explorer; retry release when mined."
                ),
            ) from exc


class DeployRequest(BaseModel):
    oracle: str = Field(..., description="Oracle address for delivery confirmation.")
    maxAmountWei: int = Field(..., gt=0, description="Maximum payment amount in wei.")
    from_address: Optional[str] = Field(None, description="Sender address for deployment.")
    private_key: Optional[str] = Field(None, description="Private key to sign the deployment transaction.")


class TransactionRequest(BaseModel):
    from_address: Optional[str] = Field(None, description="Sender address for the transaction.")
    private_key: Optional[str] = Field(None, description="Private key to sign the transaction.")


class ApproveProviderRequest(TransactionRequest):
    provider: str = Field(..., description="Provider address to approve or revoke.")
    approved: bool = Field(..., description="Approval flag for the provider.")


class SetOracleRequest(TransactionRequest):
    oracle: str = Field(..., description="New oracle address.")


class SetMaxRequest(TransactionRequest):
    maxAmountWei: int = Field(..., gt=0, description="New maximum payment amount in wei.")


class SetPausedRequest(TransactionRequest):
    paused: bool = Field(..., description="Paused state for the contract.")


class SetProviderPriceRequest(TransactionRequest):
    provider: str = Field(..., description="Provider address.")
    priceWei: int = Field(..., gt=0, description="On-chain payment amount in wei for this provider.")


class RequestResourceRequest(TransactionRequest):
    provider: str = Field(..., description="Approved provider address.")
    resourceId: str = Field(..., description="Resource identifier.")


class DepositRequest(TransactionRequest):
    amountWei: int = Field(..., gt=0, description="Amount to deposit in wei.")


class WithdrawRequest(TransactionRequest):
    amountWei: int = Field(..., gt=0, description="Amount to withdraw in wei.")


class ConfirmDeliveryRequest(TransactionRequest):
    requestId: int = Field(..., description="Request ID to confirm delivery for.")


class ReleasePaymentRequest(TransactionRequest):
    requestId: int = Field(..., description="Request ID to release payment for.")


class RefundToBuyerRequest(TransactionRequest):
    requestId: int = Field(..., description="Request ID to refund to the requester.")


@app.post("/deploy")
def deploy(deploy: DeployRequest):
    global contract
    try:
        deployer, key = get_sender_and_key(deploy.from_address, deploy.private_key)
        Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
        transaction = Contract.constructor(Web3.to_checksum_address(deploy.oracle), deploy.maxAmountWei)
        receipt = send_transaction(transaction, deployer, key)
        contract_address = receipt.contractAddress
        contract = w3.eth.contract(address=contract_address, abi=abi)
        return {
            "contractAddress": contract_address,
            "transactionHash": receipt.transactionHash.hex(),
            "chainId": CHAIN_ID,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/deposit")
def deposit(request: DepositRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.deposit(), request.from_address, request.private_key, value=request.amountWei)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/withdraw")
def withdraw(request: WithdrawRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.withdraw(request.amountWei), request.from_address, request.private_key)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/approve-provider")
def approve_provider(body: ApproveProviderRequest):
    contract = get_contract()
    provider = Web3.to_checksum_address(body.provider)
    try:
        receipt = send_transaction(
            contract.functions.approveProvider(provider, body.approved),
            body.from_address,
            body.private_key,
        )
        invalidate_approved_providers_cache(contract.address)
        on_chain_approved = contract.functions.approvedProviders(provider).call()
        if on_chain_approved != body.approved:
            raise HTTPException(
                status_code=400,
                detail=f"Transaction mined but on-chain approval is {on_chain_approved}, expected {body.approved}.",
            )
        action = "approved" if body.approved else "removed"
        return {
            "transactionHash": receipt.transactionHash.hex(),
            "status": receipt.status,
            "provider": provider,
            "approved": on_chain_approved,
            "message": f"Provider {provider} {action} on chain.",
        }
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/set-oracle")
def set_oracle(body: SetOracleRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.setOracle(Web3.to_checksum_address(body.oracle)), body.from_address, body.private_key)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/set-max-amount")
def set_max_amount(body: SetMaxRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.setMaxAmount(body.maxAmountWei), body.from_address, body.private_key)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/set-paused")
def set_paused(body: SetPausedRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.setPaused(body.paused), body.from_address, body.private_key)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def read_provider_price_wei(contract_instance, provider_address: str) -> int:
    return int(
        contract_instance.functions.providerPriceWei(
            Web3.to_checksum_address(provider_address)
        ).call()
    )


def iter_contract_event_logs(contract_instance, event: Any, from_block: int, to_block: int):
    """Yield decoded logs for one event type, scanning the chain in chunks."""
    cursor = from_block
    while cursor <= to_block:
        chunk_end = min(cursor + LOG_CHUNK_SIZE - 1, to_block)
        try:
            for log in event.get_logs(from_block=cursor, to_block=chunk_end):
                yield log
        except Exception:
            pass
        cursor = chunk_end + 1


def discover_onchain_provider_addresses(contract_instance) -> list[str]:
    """All provider addresses that appear in contract events (no off-chain catalog)."""
    from_block = get_contract_creation_block(contract_instance.address)
    to_block = w3.eth.block_number
    addresses: set[str] = set()
    for event in (
        contract_instance.events.ProviderApproved,
        contract_instance.events.ProviderPriceUpdated,
        contract_instance.events.RequestCreated,
    ):
        for log in iter_contract_event_logs(contract_instance, event, from_block, to_block):
            provider = log["args"].get("provider")
            if provider:
                addresses.add(Web3.to_checksum_address(provider))
    # Ensure currently approved providers appear even if log scan missed a chunk
    for provider in get_approved_provider_addresses(contract_instance, use_cache=False):
        addresses.add(Web3.to_checksum_address(provider))
    return sorted(addresses)


def build_provider_registry(contract_instance) -> list[dict]:
    """Registry keyed by on-chain addresses (approved list + event history)."""
    addresses: set[str] = set(get_approved_provider_addresses(contract_instance, use_cache=False))
    addresses.update(discover_onchain_provider_addresses(contract_instance))
    registry = []
    for address in sorted(addresses):
        registry.append(
            {
                "address": address,
                "approved": contract_instance.functions.approvedProviders(address).call(),
                "priceWei": read_provider_price_wei(contract_instance, address),
            }
        )
    return registry


def get_last_oracle_confirmation(contract_instance) -> Optional[dict]:
    """Latest DeliveryConfirmed event from chain (oracle activity)."""
    from_block = get_contract_creation_block(contract_instance.address)
    to_block = w3.eth.block_number
    event = contract_instance.events.DeliveryConfirmed
    latest_log = None
    for log in iter_contract_event_logs(contract_instance, event, from_block, to_block):
        latest_log = log
    if latest_log is None:
        return None
    block = w3.eth.get_block(latest_log["blockNumber"])
    confirmed_at = datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)
    return {
        "requestId": int(latest_log["args"]["requestId"]),
        "blockNumber": latest_log["blockNumber"],
        "transactionHash": latest_log["transactionHash"].hex(),
        "confirmedAt": confirmed_at.isoformat(),
    }


@app.post("/set-provider-price")
def set_provider_price(body: SetProviderPriceRequest):
    contract = get_contract()
    provider = Web3.to_checksum_address(body.provider)
    try:
        receipt = send_transaction(
            contract.functions.setProviderPrice(provider, body.priceWei),
            body.from_address,
            body.private_key,
        )
        on_chain_price = read_provider_price_wei(contract, provider)
        return {
            "transactionHash": receipt.transactionHash.hex(),
            "status": receipt.status,
            "provider": provider,
            "priceWei": on_chain_price,
            "message": f"Provider {provider} price set to {on_chain_price} wei on chain.",
        }
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/provider-price/{provider_address}")
def get_provider_price(provider_address: str):
    contract = get_contract()
    try:
        provider = Web3.to_checksum_address(provider_address)
        return {
            "provider": provider,
            "approved": contract.functions.approvedProviders(provider).call(),
            "priceWei": read_provider_price_wei(contract, provider),
            "maxAmountWei": contract.functions.maxAmountWei().call(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/request-resource")
def request_resource(body: RequestResourceRequest):
    contract = get_contract()
    provider = Web3.to_checksum_address(body.provider)
    try:
        amount_wei = read_provider_price_wei(contract, provider)
        if amount_wei <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"On-chain price for provider {provider} is not set. Use Set provider price on the dashboard.",
            )
        receipt = send_transaction(
            contract.functions.requestResource(provider, body.resourceId),
            body.from_address,
            body.private_key,
            value=amount_wei,
        )
        return {
            "transactionHash": receipt.transactionHash.hex(),
            "status": receipt.status,
            "amountWei": amount_wei,
        }
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/confirm-delivery")
def confirm_delivery(body: ConfirmDeliveryRequest):
    from agent_system.payment_flow import PaymentFlowError, assert_oracle_matches_contract, read_request_state
    from agent_system.service import get_oracle_credentials

    contract = get_contract()
    try:
        state = read_request_state(contract, body.requestId)
        if state["amountWei"] <= 0:
            raise HTTPException(status_code=400, detail="Invalid request ID")
        if state["fulfilled"]:
            return {"requestId": body.requestId, "message": "Already confirmed", "fulfilled": True}
        oracle_addr, oracle_key = get_oracle_credentials()
        if body.private_key:
            from eth_account import Account
            oracle_key = body.private_key
            oracle_addr = Web3.to_checksum_address(
                body.from_address or Account.from_key(oracle_key).address
            )
        assert_oracle_matches_contract(contract, oracle_addr)
        receipt = send_transaction(
            contract.functions.confirmDelivery(body.requestId),
            oracle_addr,
            oracle_key,
        )
        return {
            "transactionHash": receipt.transactionHash.hex(),
            "status": receipt.status,
            "requestId": body.requestId,
        }
    except PaymentFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/release-payment")
def release_payment(body: ReleasePaymentRequest):
    """Confirm (if needed) and release escrow to the provider for a request."""
    from agent_system.payment_flow import PaymentFlowError, complete_request_payment
    import backend.app as supervisor_app

    try:
        result = complete_request_payment(
            supervisor_app,
            body.requestId,
            agent_address=body.from_address,
            agent_private_key=body.private_key,
        )
        return result
    except PaymentFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/refund-to-buyer")
def refund_to_buyer(body: RefundToBuyerRequest):
    """Return escrowed payment to the requester (contract owner only)."""
    from agent_system.payment_flow import PaymentFlowError, refund_request_to_buyer
    import backend.app as supervisor_app

    try:
        return refund_request_to_buyer(supervisor_app, body.requestId)
    except PaymentFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/health")
def health_check():
    return {
        "web3Provider": WEB3_PROVIDER_URI,
        "connected": w3.is_connected(),
        "contractAddress": CONTRACT_ADDRESS,
        "contractAttached": contract is not None,
    }


@app.get("/contract")
def get_contract_state():
    contract = get_contract()
    try:
        owner = contract.functions.owner().call()
        oracle = contract.functions.oracle().call()
        paused = contract.functions.paused().call()
        max_amount = contract.functions.maxAmountWei().call()
        balance = w3.eth.get_balance(contract.address)
        request_count = contract.functions.requestCount().call()
        return {
            "address": contract.address,
            "owner": owner,
            "oracle": oracle,
            "paused": paused,
            "maxAmountWei": max_amount,
            "balanceWei": balance,
            "requestCount": request_count,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/requests/{request_id}")
def get_request(request_id: int):
    contract = get_contract()
    try:
        result = contract.functions.requests(request_id).call()
        return {
            "requestId": request_id,
            "requester": result[0],
            "provider": result[1],
            "amountWei": result[2],
            "resourceId": result[3],
            "fulfilled": result[4],
            "paid": result[5],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/provider-status/{provider_address}")
def provider_status(provider_address: str):
    contract = get_contract()
    try:
        status = contract.functions.approvedProviders(Web3.to_checksum_address(provider_address)).call()
        return {"provider": provider_address, "approved": status}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/requests-list")
def requests_list():
    contract = get_contract()
    try:
        request_count = contract.functions.requestCount().call()
        requests = []
        for req_id in range(1, request_count + 1):
            result = contract.functions.requests(req_id).call()
            requests.append({
                "requestId": req_id,
                "requester": result[0],
                "provider": result[1],
                "amountWei": result[2],
                "resourceId": result[3],
                "fulfilled": result[4],
                "paid": result[5],
            })
        return {"requests": requests, "total": request_count}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/dashboard-summary")
def dashboard_summary():
    contract = get_contract()
    try:
        owner = contract.functions.owner().call()
        oracle = contract.functions.oracle().call()
        paused = contract.functions.paused().call()
        max_amount = contract.functions.maxAmountWei().call()
        balance = w3.eth.get_balance(contract.address)
        request_count = contract.functions.requestCount().call()
        
        # Get recent requests (last 5)
        recent_requests = []
        start = max(1, request_count - 4)
        for req_id in range(start, request_count + 1):
            result = contract.functions.requests(req_id).call()
            recent_requests.append({
                "requestId": req_id,
                "requester": result[0],
                "provider": result[1],
                "amountWei": result[2],
                "resourceId": result[3],
                "fulfilled": result[4],
                "paid": result[5],
            })
        
        approved_providers = get_approved_provider_addresses(contract, use_cache=False)
        provider_registry = build_provider_registry(contract)
        last_oracle_confirmation = get_last_oracle_confirmation(contract)
        from agent_system.payment_flow import list_pending_requests

        pending_payments = list_pending_requests(contract)

        return {
            "address": contract.address,
            "owner": owner,
            "oracle": oracle,
            "paused": paused,
            "maxAmountWei": max_amount,
            "balanceWei": balance,
            "requestCount": request_count,
            "recentRequests": recent_requests,
            "approvedProviders": approved_providers,
            "providers": provider_registry,
            "lastOracleConfirmation": last_oracle_confirmation,
            "pendingPayments": pending_payments,
            "pendingPaymentCount": len(pending_payments),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
