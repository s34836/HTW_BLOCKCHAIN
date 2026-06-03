import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from agent_system.payment_flow import PaymentFlowError, complete_request_payment, list_pending_requests
from agent_system.purchase_flow import PurchaseError, run_purchase_random_numbers
from agent_system.service import (
    discover_provider_services,
    enrich_catalog_with_approval,
    get_agent_credentials,
)
from agent_system.settings import agent_settings

router = APIRouter(tags=["agent"])


class ChatMessage(BaseModel):
    role: str = Field(..., description="user or assistant")
    content: str = Field(..., description="Message text")


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User message")
    history: list[ChatMessage] = Field(default_factory=list, description="Prior chat turns")


class AgentPurchaseRequest(BaseModel):
    count: int = Field(..., description="Number of random integers required (5 or 10).")
    amountWei: Optional[int] = Field(
        None,
        description="Deprecated override; default is on-chain providerPriceWei.",
    )
    agent_address: Optional[str] = Field(None, description="Override agent wallet address.")
    agent_private_key: Optional[str] = Field(None, description="Override agent signing key.")
    oracle_private_key: Optional[str] = Field(None, description="Override oracle signing key.")


def _supervisor():
    import backend.app as supervisor

    return supervisor


@router.post("/agent/chat")
async def agent_chat(body: AgentChatRequest):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured. Add it to .env to use the chat agent.",
        )
    try:
        from agent_system.chat import run_chat

        result = await run_chat(
            body.message,
            history=[item.model_dump() for item in body.history],
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/agent/chat/health")
def agent_chat_health():
    return {
        "openaiConfigured": bool(os.getenv("OPENAI_API_KEY")),
        "model": agent_settings.openai_model,
        "agentAddress": agent_settings.agent_address,
    }


@router.get("/agent/provider-catalog")
def agent_provider_catalog():
    """Discover independent provider backends and on-chain approval flags."""
    supervisor = _supervisor()
    contract = supervisor.get_contract()
    catalog = discover_provider_services()
    return {
        "agentAddress": get_agent_credentials()[0],
        "providers": enrich_catalog_with_approval(contract, catalog),
    }


@router.post("/agent/purchase-random-numbers")
def agent_purchase_random_numbers(body: AgentPurchaseRequest):
    try:
        return run_purchase_random_numbers(
            count=body.count,
            amount_wei=body.amountWei,
            agent_address=body.agent_address,
            agent_private_key=body.agent_private_key,
            oracle_private_key=body.oracle_private_key,
        )
    except PurchaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/agent/pending-payments")
def agent_pending_payments():
    supervisor = _supervisor()
    contract = supervisor.get_contract()
    pending = list_pending_requests(contract)
    return {"pending": pending, "count": len(pending)}


@router.post("/agent/complete-request/{request_id}")
def agent_complete_request(request_id: int):
    supervisor = _supervisor()
    try:
        return complete_request_payment(supervisor, request_id)
    except PaymentFlowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/agent/complete-pending-payments")
def agent_pending_payments_status():
    """Fast check: list unpaid requests (does not send transactions)."""
    supervisor = _supervisor()
    contract = supervisor.get_contract()
    pending = list_pending_requests(contract)
    return {"pending": pending, "count": len(pending)}


@router.post("/agent/complete-pending-payments")
def agent_complete_all_pending():
    """Release one pending payment per call — use complete-request in a loop from the UI."""
    supervisor = _supervisor()
    contract = supervisor.get_contract()
    pending = list_pending_requests(contract)
    if not pending:
        return {"completed": [], "errors": [], "message": "No pending payments."}
    item = pending[0]
    request_id = int(item["requestId"])
    try:
        result = complete_request_payment(supervisor, request_id)
        return {
            "completed": [result],
            "errors": [],
            "remaining": len(pending) - 1,
            "message": (
                f"Released request #{request_id}. "
                f"{max(0, len(pending) - 1)} remaining — click again or use per-request API."
            ),
        }
    except PaymentFlowError as exc:
        return {
            "completed": [],
            "errors": [{"requestId": request_id, "error": str(exc)}],
            "remaining": len(pending),
            "message": f"Failed to release request #{request_id}: {exc}",
        }
