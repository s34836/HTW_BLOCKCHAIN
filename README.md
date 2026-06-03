# HTW_BLOCKCHAIN

This repository contains a Solidity contract and a Python backend for managing it.

## Python Backend

The backend exposes HTTP endpoints to deploy and interact with the `AIAgentMicropayment` contract defined in `contract.sol`.

### Install dependencies

```bash
python -m pip install -r requirements.txt
```

### Backend configuration

The backend reads configuration from the `.env` file in the repository root.

Create or update `.env` with your actual values.

#### Example: Sepolia deployed contract

If your deployed contract is `0x96800541bbf413301be6ea89c3304493ea43ae51`, set:

```bash
WEB3_PROVIDER_URI=https://sepolia.infura.io/v3/<YOUR_INFURA_KEY>
CHAIN_ID=11155111
CONTRACT_ADDRESS=0x96800541bbf413301be6ea89c3304493ea43ae51
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

### Run the backend

```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

### Open the dashboard

After the backend starts, open the dashboard in your browser:

```bash
http://127.0.0.1:8000/
```

The frontend is served by the Python backend and communicates with the same FastAPI API.

### Configuration options

The backend reads configuration from `.env` and environment variables.

- `WEB3_PROVIDER_URI` - HTTP provider URL for Ethereum node (default `http://127.0.0.1:8545`)
- `CHAIN_ID` - Chain ID for transactions (default `1337`)
- `DEFAULT_FROM_ADDRESS` - Default sender address for transaction calls
- `OWNER_PRIVATE_KEY` - Private key used to sign transactions if accounts are not unlocked
- `CONTRACT_ADDRESS` - Existing deployed contract address to attach to

Values set in the environment override `.env` values.

### Example endpoints

- `POST /deploy`
- `POST /deposit`
- `POST /withdraw`
- `POST /approve-provider`
- `POST /request-resource`
- `POST /confirm-delivery`
- `GET /contract`
- `GET /requests/{requestId}`
