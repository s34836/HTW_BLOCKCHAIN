from pathlib import Path
from typing import Optional

import solcx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from config import settings

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

ROOT_DIR = Path(__file__).resolve().parent
frontend_dir = ROOT_DIR / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

w3 = Web3(Web3.HTTPProvider(WEB3_PROVIDER_URI))


def ensure_web3_connected():
    if not w3.is_connected():
        raise HTTPException(status_code=503, detail=f"Unable to connect to Web3 provider at {WEB3_PROVIDER_URI}")


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


def get_sender_and_key(from_address: Optional[str], private_key: Optional[str]):
    ensure_web3_connected()
    sender = from_address or DEFAULT_FROM_ADDRESS
    if sender:
        sender = Web3.to_checksum_address(sender)
    elif OWNER_PRIVATE_KEY or private_key:
        key = private_key or OWNER_PRIVATE_KEY
        account = w3.eth.account.from_key(key)
        sender = account.address
    else:
        accounts = w3.eth.accounts
        if not accounts:
            raise HTTPException(status_code=400, detail="No sender address available. Set DEFAULT_FROM_ADDRESS or OWNER_PRIVATE_KEY.")
        sender = accounts[0]
    key = private_key or OWNER_PRIVATE_KEY
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
        tx = function.build_transaction({
            **tx_params,
            "nonce": w3.eth.get_transaction_count(sender),
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, key)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    else:
        tx_hash = function.transact(tx_params)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt


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


class RequestResourceRequest(TransactionRequest):
    provider: str = Field(..., description="Approved provider address.")
    amount: int = Field(..., gt=0, description="Payment amount in wei.")
    resourceId: str = Field(..., description="Resource identifier.")


class DepositRequest(TransactionRequest):
    amountWei: int = Field(..., gt=0, description="Amount to deposit in wei.")


class WithdrawRequest(TransactionRequest):
    amountWei: int = Field(..., gt=0, description="Amount to withdraw in wei.")


class ConfirmDeliveryRequest(TransactionRequest):
    requestId: int = Field(..., description="Request ID to confirm delivery for.")


class ReleasePaymentRequest(TransactionRequest):
    requestId: int = Field(..., description="Request ID to release payment for.")


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
    try:
        receipt = send_transaction(
            contract.functions.approveProvider(Web3.to_checksum_address(body.provider), body.approved),
            body.from_address,
            body.private_key,
        )
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
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


@app.post("/request-resource")
def request_resource(body: RequestResourceRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(
            contract.functions.requestResource(
                Web3.to_checksum_address(body.provider),
                body.amount,
                body.resourceId,
            ),
            body.from_address,
            body.private_key,
        )
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/confirm-delivery")
def confirm_delivery(body: ConfirmDeliveryRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.confirmDelivery(body.requestId), body.from_address, body.private_key)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/release-payment")
def release_payment(body: ReleasePaymentRequest):
    contract = get_contract()
    try:
        receipt = send_transaction(contract.functions.releasePayment(body.requestId), body.from_address, body.private_key)
        return {"transactionHash": receipt.transactionHash.hex(), "status": receipt.status}
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
        
        return {
            "address": contract.address,
            "owner": owner,
            "oracle": oracle,
            "paused": paused,
            "maxAmountWei": max_amount,
            "balanceWei": balance,
            "requestCount": request_count,
            "recentRequests": recent_requests,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
