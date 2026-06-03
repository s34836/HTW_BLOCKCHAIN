"""LLM chat agent with tools for provider discovery and on-chain purchase."""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from agent_backend.settings import agent_settings
from agent_backend.tools import list_available_providers, purchase_random_numbers

CHAT_SYSTEM_PROMPT = """You are a micropayment AI assistant for an HTW blockchain demo on Sepolia.

You help users understand data providers and obtain random number datasets when they need them.

Tools:
- list_available_providers — shows which independent providers exist, how many numbers each offers (5 or 10), and on-chain approval status.
- purchase_random_numbers(count) — agent pays providerPriceWei to the contract on requestResource, fetches data, oracle confirms delivery, then releasePayment pays the provider (count must be 5 or 10).

Guidelines:
- When the user needs random numbers (analysis, simulation, examples, "random", "draw", "lottery", etc.), prefer using purchase_random_numbers rather than inventing numbers.
- Choose count=10 when they need more data, ~10 values, or a larger sample; choose count=5 when they need fewer or ~5 values. If ambiguous, ask one short clarifying question, then purchase.
- After a successful purchase, clearly show the numbers and mention that payment was made on-chain.
- Use list_available_providers when the user asks what is available, who provides data, or before purchasing if context is unclear.
- Do not purchase without intent to obtain random data from providers.
- Be concise and helpful. Always respond in English.
"""

_chat_agent = None


def get_chat_agent():
    global _chat_agent
    if _chat_agent is None:
        _chat_agent = create_react_agent(
            ChatOpenAI(model=agent_settings.openai_model, temperature=0.2),
            tools=[list_available_providers, purchase_random_numbers],
            prompt=CHAT_SYSTEM_PROMPT,
        )
    return _chat_agent


def _history_to_messages(history: list[dict]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for item in history:
        role = item.get("role", "")
        content = str(item.get("content", ""))
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    return messages


def _messages_to_history(messages: list[BaseMessage]) -> list[dict]:
    history: list[dict] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": message.content})
        elif isinstance(message, AIMessage) and message.content:
            history.append({"role": "assistant", "content": message.content})
    return history


async def run_chat(message: str, history: list[dict] | None = None) -> dict:
    history = history or []
    agent = get_chat_agent()
    messages = _history_to_messages(history) + [HumanMessage(content=message)]
    result = await agent.ainvoke({"messages": messages})
    all_messages = result["messages"]
    reply = ""
    for msg in reversed(all_messages):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break
    updated_history = _messages_to_history(all_messages)
    return {
        "reply": reply,
        "history": updated_history,
        "agentAddress": agent_settings.agent_address,
    }
