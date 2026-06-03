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
from web3.exceptions import ContractLogicError, Web3RPCError

from agent_backend import agent_router
from backend_supervisor.config import settings
from htw_logging import attach_request_logging, setup_service_logging

ROOT_DIR = Path(__file__).resolve().parent
REPO_ROOT = ROOT_DIR.parent
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

_agent_chat_origin = os.getenv("AGENT_CHAT_ORIGIN", "http://127.0.0.1:8003")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_agent_chat_origin, "http://localhost:8003"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger = attach_request_logging(app, "supervisor")
setup_service_logging("agent")

frontend_dir = REPO_ROOT / "frontend_supervisor"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")
app.include_router(agent_router)

w3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER_URI))

PROVIDER_APPROVED_TOPIC = Web3.keccak(text="ProviderApproved(address,bool)")
LOG_CHUNK_SIZE = 9_000
CREATION_BLOCK_CACHE: dict[str, int] = {}
APPROVED_PROVIDERS_CACHE: dict[str, list[str]] = {}


@app.on_event("startup")
def log_supervisor_startup():
    logger.info(
        "Supervisor API ready | contract=%s | chainId=%s | rpc=%s",
        CONTRACT_ADDRESS or "(not set)",
        CHAIN_ID,
        WEB3_PROVIDER_URI[:48] + "..." if len(WEB3_PROVIDER_URI) > 48 else WEB3_PROVIDER_URI,
    )
TX_RECEIPT_TIMEOUT_SEC = int(os.getenv("TX_RECEIPT_TIMEOUT_SEC", "180"))
TX_PROPAGATION_TIMEOUT_SEC = int(os.getenv("TX_PROPAGATION_TIMEOUT_SEC", "25"))
MIN_SENDER_BALANCE_WEI = int(os.getenv("MIN_SENDER_BALANCE_WEI", str(Web3.to_wei(0.0005, "ether"))))
SEPOLIA_TX_EXPLORER = "https://sepolia.etherscan.io/tx/"


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
    from eth_account import Account

    ensure_web3_connected()
    sender = from_address or DEFAULT_FROM_ADDRESS
    key = private_key or OWNER_PRIVATE_KEY
    if sender:
        sender = Web3.to_checksum_address(sender)
    elif key:
        sender = Web3.to_checksum_address(Account.from_key(key).address)
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
    if key:
        derived = Web3.to_checksum_address(Account.from_key(key).address)
        if derived != sender:
            raise HTTPException(
                status_code=400,
                detail=f"Signer mismatch: private key is for {derived}, but from address is {sender}.",
            )
    return sender, key


def _tx_hex(tx_hash) -> str:
    raw = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return raw if raw.startswith("0x") else f"0x{raw}"


def _ensure_sender_can_pay_gas(sender: str) -> None:
    balance = w3.eth.get_balance(sender)
    if balance < MIN_SENDER_BALANCE_WEI:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Wallet {sender} has only {balance} wei for gas on Sepolia. "
                f"Fund it with test ETH (recommended at least {MIN_SENDER_BALANCE_WEI} wei)."
            ),
        )


def _fee_params(gas_multiplier: float) -> dict[str, int]:
    """Legacy gasPrice — reliable on Sepolia; multiplier bumps replacements."""
    multiplier = max(gas_multiplier, 1.15)
    network_gas = max(int(w3.eth.gas_price), int(w3.to_wei(2, "gwei")))
    return {"gasPrice": int(network_gas * multiplier)}


def _next_on_chain_nonce(sender: str) -> int:
    """Next nonce from confirmed chain state (matches Etherscan), not RPC pending pool."""
    latest = w3.eth.get_transaction_count(sender, "latest")
    pending = w3.eth.get_transaction_count(sender, "pending")
    if pending > latest:
        logger.warning(
            "Ignoring RPC pending nonce %s > on-chain next %s for %s (stale mempool at provider)",
            pending,
            latest,
            sender,
        )
    return latest


def _estimate_contract_gas(function, tx_params: dict[str, Any]) -> int:
    try:
        estimated = function.estimate_gas(tx_params)
        return max(int(estimated * 1.25), 120_000)
    except Exception as exc:
        logger.warning("Gas estimate failed, using default cap: %s", exc)
        return 500_000


def _wait_for_propagation(tx_hash, sender: str) -> None:
    """Fail fast when RPC returns a hash but the tx never hits the network."""
    deadline = time.monotonic() + TX_PROPAGATION_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if _chain_tx_status(tx_hash) != "missing":
            return
        time.sleep(2)
    _raise_timeout_http_error(tx_hash, sender)


def _chain_tx_status(tx_hash) -> str:
    """missing | pending | success | reverted"""
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception:
        return "missing"
    if tx is None:
        return "missing"
    block_number = tx.get("blockNumber") if hasattr(tx, "get") else getattr(tx, "blockNumber", None)
    if block_number is None:
        return "pending"
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        return "success" if int(receipt["status"]) == 1 else "reverted"
    except Exception:
        return "pending"


def _raise_timeout_http_error(tx_hash, sender: str) -> None:
    tx_hex = _tx_hex(tx_hash)
    explorer = f"{SEPOLIA_TX_EXPLORER}{tx_hex}"
    status = _chain_tx_status(tx_hash)

    if status == "missing":
        next_nonce = _next_on_chain_nonce(sender)
        raise HTTPException(
            status_code=502,
            detail=(
                f"Transaction {tx_hex} never appeared on Sepolia (dropped or not broadcast). "
                f"Safe to try again — next on-chain nonce for {sender} is {next_nonce}. "
                f"Explorer: {explorer}"
            ),
        )
    if status == "pending":
        raise HTTPException(
            status_code=504,
            detail=(
                f"Transaction {tx_hex} is still pending after {TX_RECEIPT_TIMEOUT_SEC}s. "
                f"Wait for it to confirm before sending another action. Explorer: {explorer}"
            ),
        )
    if status == "reverted":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Transaction {tx_hex} was mined but reverted. "
                f"Check contract state and wallet roles. Explorer: {explorer}"
            ),
        )
    raise HTTPException(
        status_code=504,
        detail=(
            f"Transaction {tx_hex} not confirmed within {TX_RECEIPT_TIMEOUT_SEC}s. "
            f"Refresh the dashboard in a minute. Explorer: {explorer}"
        ),
    )


def _rpc_error_message(exc: Exception) -> str:
    if isinstance(exc, Web3RPCError):
        payload = exc.args[0] if exc.args else exc
        if isinstance(payload, dict):
            return str(payload.get("message", payload))
    return str(exc)


def _is_retriable_rpc_error(exc: Exception) -> bool:
    msg = _rpc_error_message(exc).lower()
    return any(
        phrase in msg
        for phrase in (
            "replacement transaction underpriced",
            "transaction underpriced",
            "nonce too low",
            "already known",
        )
    )


def send_transaction(function, from_address: Optional[str] = None, private_key: Optional[str] = None, value: int = 0):
    ensure_web3_connected()
    sender, key = get_sender_and_key(from_address, private_key)
    _ensure_sender_can_pay_gas(sender)
    base_tx_params = {
        "from": sender,
        "value": value,
        "chainId": CHAIN_ID,
    }
    if key:
        gas_multiplier = 1.15
        last_error: Optional[Exception] = None
        replace_nonce: Optional[int] = None
        last_nonce: Optional[int] = None
        for attempt in range(3):
            try:
                nonce = replace_nonce if replace_nonce is not None else _next_on_chain_nonce(sender)
                last_nonce = nonce
                fee_fields = _fee_params(gas_multiplier)
                build_params = {
                    **base_tx_params,
                    "nonce": nonce,
                    "gas": _estimate_contract_gas(function, {**base_tx_params, "nonce": nonce}),
                    **fee_fields,
                }
                tx = function.build_transaction(build_params)
                signed = w3.eth.account.sign_transaction(tx, key)
                tx_hash = w3.eth.send_raw_transaction(_raw_transaction_bytes(signed))
                logger.info(
                    "Sent tx %s from %s nonce=%s gasPrice=%s (attempt %s)",
                    _tx_hex(tx_hash),
                    sender,
                    nonce,
                    fee_fields.get("gasPrice"),
                    attempt + 1,
                )
                _wait_for_propagation(tx_hash, sender)
                receipt = _wait_for_receipt(
                    tx_hash,
                    sender=sender,
                    skip_propagation_check=True,
                )
                if receipt.status != 1:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Transaction {_tx_hex(tx_hash)} reverted on chain. "
                            f"Explorer: {SEPOLIA_TX_EXPLORER}{_tx_hex(tx_hash)}"
                        ),
                    )
                return receipt
            except HTTPException as exc:
                detail = str(exc.detail)
                if exc.status_code == 502 and "never appeared on Sepolia" in detail and attempt < 2:
                    replace_nonce = None
                    gas_multiplier *= 1.25
                    logger.warning(
                        "Retrying %s with on-chain nonce %s (attempt %s, gas x%.2f)",
                        sender,
                        _next_on_chain_nonce(sender),
                        attempt + 2,
                        gas_multiplier,
                    )
                    time.sleep(1.0)
                    continue
                if exc.status_code == 504 and "still pending" in detail:
                    raise
                raise
            except Exception as exc:
                last_error = exc
                if _is_retriable_rpc_error(exc) and attempt < 2:
                    replace_nonce = last_nonce
                    gas_multiplier *= 1.3
                    logger.warning(
                        "RPC retry for %s nonce=%s (attempt %s): %s",
                        sender,
                        replace_nonce,
                        attempt + 2,
                        _rpc_error_message(exc),
                    )
                    time.sleep(1.0)
                    continue
                break
        detail = _rpc_error_message(last_error) if last_error else "Unknown RPC error"
        if "replacement transaction underpriced" in detail.lower():
            detail += (
                f" Check pending txs for {sender} on Sepolia Etherscan before retrying."
            )
        raise HTTPException(status_code=502, detail=f"RPC error: {detail}") from last_error
    tx_params = {**base_tx_params, "gas": 500_000}
    tx_hash = function.transact(tx_params)
    _wait_for_propagation(tx_hash, sender)
    receipt = _wait_for_receipt(tx_hash, sender=sender, skip_propagation_check=True)
    if receipt.status != 1:
        raise HTTPException(status_code=400, detail="Transaction reverted on chain. Check owner wallet, gas, and contract state.")
    return receipt


def _wait_for_receipt(
    tx_hash,
    timeout: int = TX_RECEIPT_TIMEOUT_SEC,
    *,
    sender: str = "",
    skip_propagation_check: bool = False,
):
    """Wait for mining; on timeout inspect chain and return a precise error."""
    from web3.exceptions import TimeExhausted

    if not skip_propagation_check:
        _wait_for_propagation(tx_hash, sender or "unknown")

    try:
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout, poll_latency=2)
    except TimeExhausted as exc:
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            if _chain_tx_status(tx_hash) == "success":
                try:
                    return w3.eth.get_transaction_receipt(tx_hash)
                except Exception:
                    pass
            _raise_timeout_http_error(tx_hash, sender or "unknown")
            raise AssertionError("unreachable") from exc


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
    from agent_backend.payment_flow import PaymentFlowError, assert_oracle_matches_contract, read_request_state
    from agent_backend.service import get_oracle_credentials

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
    logger.info("Release payment for request #%s", body.requestId)
    from agent_backend.payment_flow import PaymentFlowError, complete_request_payment
    import backend_supervisor.app as supervisor_app

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
    logger.info("Refund to buyer for request #%s", body.requestId)
    from agent_backend.payment_flow import PaymentFlowError, refund_request_to_buyer
    import backend_supervisor.app as supervisor_app

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
    logger.info("Loading dashboard summary from chain")
    contract = get_contract()
    try:
        owner = contract.functions.owner().call()
        oracle = contract.functions.oracle().call()
        paused = contract.functions.paused().call()
        max_amount = contract.functions.maxAmountWei().call()
        balance = w3.eth.get_balance(contract.address)
        request_count = contract.functions.requestCount().call()
        
        # All requests (newest last in list; frontend sorts for display)
        recent_requests = []
        for req_id in range(1, request_count + 1):
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
        from agent_backend.payment_flow import list_pending_requests

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
