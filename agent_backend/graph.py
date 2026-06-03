import json
import re
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent

from agent_backend.settings import agent_settings
from agent_backend.tools import list_available_providers, purchase_random_numbers


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    requested_count: int | None


def extract_count(text: str) -> int | None:
    if re.search(r"\b10\b|ten\b", text, re.I):
        return 10
    if re.search(r"\b5\b|five\b", text, re.I):
        return 5
    return None


def build_catalog_agent():
    return create_react_agent(
        ChatOpenAI(model=agent_settings.openai_model, temperature=0),
        tools=[list_available_providers],
        prompt=(
            "You are the Catalog Agent. "
            "Use list_available_providers to explain which providers exist, "
            "how many random numbers each offers, and whether they are approved on chain. "
            "Be concise and factual."
        ),
    )


def build_procurement_agent():
    return create_react_agent(
        ChatOpenAI(model=agent_settings.openai_model, temperature=0),
        tools=[purchase_random_numbers],
        prompt=(
            "You are the Procurement Agent for Sepolia micropayments. "
            "When the user needs random numbers, call purchase_random_numbers with count 5 or 10. "
            "Report the returned numbers and transaction hashes."
        ),
    )


catalog_agent = build_catalog_agent()
procurement_agent = build_procurement_agent()


def router_node(state: AgentState) -> AgentState:
    last_human = next((message.content for message in reversed(state["messages"]) if isinstance(message, HumanMessage)), "")
    count = extract_count(str(last_human))
    return {"requested_count": count}


def route_after_router(state: AgentState) -> Literal["catalog", "procurement"]:
    last_human = next((message.content for message in reversed(state["messages"]) if isinstance(message, HumanMessage)), "")
    text = str(last_human).lower()
    if state.get("requested_count") in (5, 10):
        return "procurement"
    if any(word in text for word in ("provider", "catalog", "available", "supply", "who", "what")):
        return "catalog"
    return "catalog"


async def catalog_node(state: AgentState) -> AgentState:
    result = await catalog_agent.ainvoke({"messages": state["messages"]})
    return {"messages": result["messages"]}


async def procurement_node(state: AgentState) -> AgentState:
    count = state.get("requested_count")
    if count not in (5, 10):
        count = extract_count(str(state["messages"][-1].content if state["messages"] else ""))
    if count not in (5, 10):
        return {
            "messages": [
                AIMessage(content="Specify whether you need 5 or 10 random numbers so I can purchase them on chain.")
            ]
        }

    instruction = HumanMessage(content=f"Purchase exactly {count} random numbers using purchase_random_numbers.")
    result = await procurement_agent.ainvoke({"messages": state["messages"] + [instruction]})
    return {"messages": result["messages"]}


def build_multi_agent_graph():
    graph = StateGraph(AgentState)
    graph.add_node("router", router_node)
    graph.add_node("catalog", catalog_node)
    graph.add_node("procurement", procurement_node)
    graph.set_entry_point("router")
    graph.add_conditional_edges("router", route_after_router, {"catalog": "catalog", "procurement": "procurement"})
    graph.add_edge("catalog", END)
    graph.add_edge("procurement", END)
    return graph.compile()


async def run_multi_agent(user_query: str) -> dict:
    from agent_backend.chat import run_chat

    result = await run_chat(user_query, history=[])
    return {
        "query": user_query,
        "requested_count": extract_count(user_query),
        "answer": result["reply"],
        "history": result["history"],
    }


def run_direct_purchase(count: int) -> dict:
    """Run without LLM (useful when OPENAI_API_KEY is not set)."""
    import httpx

    from agent_backend.purchase_flow import PurchaseError, run_purchase_random_numbers

    base = agent_settings.supervisor_url.rstrip("/")
    with httpx.Client(timeout=agent_settings.http_timeout) as client:
        catalog_response = client.get(f"{base}/agent/provider-catalog")
        catalog_response.raise_for_status()
        catalog = catalog_response.json()
    try:
        purchase = run_purchase_random_numbers(count=count)
    except PurchaseError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"mode": "direct", "catalog": catalog, "purchase": purchase}
