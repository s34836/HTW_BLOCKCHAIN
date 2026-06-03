# HTW_BLOCKCHAIN

This repository contains a Solidity contract and a Python backend for managing it.

## Project layout

| Path | Role |
|------|------|
| `backend/` | Supervisor API + admin dashboard (`frontend/`), `contract.sol` |
| `agent_system/` | LangChain agent (mounted on supervisor, CLI) |
| `agent_frontend/` | Agent chat UI (port 8003) |
| `providers/` | Independent data provider APIs |

`.env` stays in the repository root.

## Python Backend

The supervisor backend lives in `backend/` and exposes HTTP endpoints to deploy and interact with the `AIAgentMicropayment` contract defined in `backend/contract.sol`.

### Install dependencies

```bash
python -m pip install -r backend/requirements.txt
```

### Backend configuration

The backend reads configuration from the `.env` file in the repository root.

Create or update `.env` with your actual values.

#### Example: Sepolia deployed contract

If your deployed contract is `0xcc7256f8c58ec0B9a4FdC306729898804996119a`, set:

```bash
WEB3_PROVIDER_URI=https://sepolia.infura.io/v3/<YOUR_INFURA_KEY>
CHAIN_ID=11155111
CONTRACT_ADDRESS=0xcc7256f8c58ec0B9a4FdC306729898804996119a
```

If you use Infura, make sure you use the Sepolia endpoint from your Infura project dashboard, not the Mainnet URL.

The screenshot you shared shows a Mainnet endpoint (`https://mainnet.infura.io/v3/...`) — that is wrong for Sepolia.

Example Infura Sepolia URL:
```bash
https://sepolia.infura.io/v3/<YOUR_PROJECT_ID>
```

Replace the final segment with your actual Infura Project ID.

If you prefer not to use Infura, you can also use public Sepolia RPC:
```bash
WEB3_PROVIDER_URI=https://rpc.ankr.com/eth_sepolia
# or
WEB3_PROVIDER_URI=https://sepolia.public-rpc.com
```

> Note: `WEB3_PROVIDER_URI=http://127.0.0.1:8545` is for a local Ethereum node, not Sepolia.
> For Sepolia, you must use a real Sepolia RPC endpoint and chain ID `11155111`.
> `OWNER_PRIVATE_KEY` must be a real signing key, not an address.

### Run the stack

Four separate backends (supervisor, agent chat UI, two data providers):

```bash
# Terminal 1 — Data Provider Alpha (10 random numbers)
.venv/bin/uvicorn providers.alpha_app:app --host 0.0.0.0 --port 8001

# Terminal 2 — Data Provider Beta (5 random numbers)
.venv/bin/uvicorn providers.beta_app:app --host 0.0.0.0 --port 8002

# Terminal 3 — Supervisor / micropayment API + dashboard
.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000

# Terminal 4 — Agent chat UI (calls supervisor API)
.venv/bin/uvicorn agent_frontend.app:app --host 0.0.0.0 --port 8003
```

### Open the dashboard

After the backend starts, open the dashboard in your browser:

```bash
http://127.0.0.1:8000/
```

**Agent chat (LLM):** [http://127.0.0.1:8003/](http://127.0.0.1:8003/) — standalone UI on port **8003**; it calls the supervisor API on **8000** (`POST /agent/chat`). The model decides when to buy 5 or 10 random numbers on-chain.

The supervisor dashboard is on port 8000; the agent chat frontend is a separate process.

### Configuration options

The backend reads configuration from `.env` and environment variables.

- `WEB3_PROVIDER_URI` - HTTP provider URL for Ethereum node (default `http://127.0.0.1:8545`)
- `CHAIN_ID` - Chain ID for transactions (default `1337`)
- `DEFAULT_FROM_ADDRESS` - Default sender address for transaction calls
- `OWNER_PRIVATE_KEY` - Private key used to sign transactions if accounts are not unlocked
- `CONTRACT_ADDRESS` - Existing deployed contract address to attach to
Values set in the environment override `.env` values.

Agent-related variables (`AGENT_ADDRESS`, `AGENT_PRIVATE_KEY`, `ORACLE_PRIVATE_KEY`, `PROVIDER_ALPHA_URL`, `PROVIDER_BETA_URL`, `OPENAI_API_KEY`, …) are read from `.env` by `agent_system/settings.py`.

**Per-provider payment (on-chain):** the supervisor sets `providerPriceWei` per provider address via the dashboard (**Set on-chain price**) or `POST /set-provider-price`. Agents read that mapping when calling `requestResource` — not from `.env`. After upgrading the contract, **redeploy** and set prices again.

Agent chat UI (port 8003): `SUPERVISOR_API_URL` (default `http://127.0.0.1:8000`), `AGENT_CHAT_PORT` (default `8003`). Supervisor CORS: `AGENT_CHAT_ORIGIN` (default `http://127.0.0.1:8003`).

### Data providers (fully separate backends)

Provider apps live under `providers/` and are **not** imported by the supervisor. Run them as standalone services:

| Provider | Port | Address | API |
|----------|------|---------|-----|
| Data Provider Alpha | 8001 | `0xb91A1B6Fb3d910710984c301Cb162460Aef3b209` | `GET /random-numbers` → 10 integers |
| Data Provider Beta | 8002 | `0xDaaA2F9b185c1D88D19Fc63d8D4480D5459b9308` | `GET /random-numbers` → 5 integers |
| Agent chat UI | 8003 | — | Static UI; API → supervisor `:8000` `/agent/chat` |

```bash
curl http://127.0.0.1:8001/random-numbers
curl http://127.0.0.1:8002/random-numbers
```

The supervisor dashboard lists providers **only from on-chain contract events** (not from this table). Approve provider addresses and set `providerPriceWei` in the dashboard before agents call `request-resource`.

**Payment flow:** the agent sends ETH with `requestResource` (`msg.value` = on-chain `providerPriceWei`). The contract does not need a pre-funded balance. After the agent fetches data, the oracle calls `confirmDelivery`, then `releasePayment` sends ETH to the provider. Optional: owner `POST /deposit` for a shared pool.

### LangChain multi-agent system (`agent_system/`)

All agent code lives under `agent_system/`:

| File | Role |
|------|------|
| `api.py` | Supervisor routes `/agent/*` (mounted from `backend/app.py`) |
| `service.py` | Provider discovery + micropayment orchestration |
| `tools.py` / `graph.py` | LangChain tools and LangGraph workflow |
| `settings.py` | Agent env configuration |
| `__main__.py` | CLI entry point |

Install agent dependencies:

```bash
python -m pip install -r agent_system/requirements.txt
```

Two agents (LangGraph):

1. **Catalog Agent** — asks supervisor `GET /agent/provider-catalog`
2. **Procurement Agent** — buys data via `POST /agent/purchase-random-numbers` (agent pays on `requestResource` → fetch provider → oracle `confirmDelivery` → `releasePayment` to provider)

Agent wallet (default `0x71FE831B3ef3a61e0EAFed83A3da31d2f08D4079`) signs `requestResource` (with ETH) and `releasePayment`. Oracle key confirms delivery.

```bash
# LLM mode (needs OPENAI_API_KEY)
python -m agent_system "What providers are available?"
python -m agent_system "I need 10 random numbers"

# Direct mode without OpenAI
python -m agent_system --count 10
python -m agent_system --count 5
```

Agent API on supervisor:

- `GET /agent/chat/health`
- `POST /agent/chat` — body `{"message": "...", "history": [{"role":"user","content":"..."}]}`
- `GET /agent/provider-catalog`
- `POST /agent/purchase-random-numbers` with body `{"count": 5}` or `{"count": 10}`

### Example endpoints

- `POST /deploy`
- `POST /deposit`
- `POST /withdraw`
- `POST /approve-provider`
- `POST /set-provider-price` — body `{"provider": "0x...", "priceWei": 1000}`
- `GET /provider-price/{address}`
- `POST /request-resource` — agent pays `providerPriceWei` in the same tx (body: `provider`, `resourceId`)
- `POST /confirm-delivery`
- `POST /release-payment` — confirms (if needed) and releases escrow to provider
- `POST /agent/complete-pending-payments` — finish all unpaid requests (stuck escrow)
- `GET /agent/pending-payments`
- `GET /agent/provider-catalog`
- `POST /agent/purchase-random-numbers`
- `GET /contract`
- `GET /requests/{requestId}`
