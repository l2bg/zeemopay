# ReqCast

Payment infrastructure for AI tools. Your tool gets a dedicated payment endpoint immediately. ReqCast sits between AI agents and any registered paid API tool. Every transaction passes through ReqCast. ReqCast verifies the payment on-chain, triggers the tool, writes a receipt, and pays the developer 95% instantly in USDC.

## What This Repo Contains

This is the complete backend. All five endpoints live in `main.py`. The payment rail is x402 USDC on Base L2 via Coinbase. Tools and transactions are persisted in Supabase. The split mechanic is proved and working on Base Sepolia testnet with real on-chain transaction hashes.

## Stack

- Python 3.12
- FastAPI + Uvicorn
- x402 v2.3.0 (HTTP payment middleware)
- Coinbase CDP SDK (wallet and transaction management)
- Supabase (persistent storage for tools and receipts)
- Base Sepolia testnet (eip155:84532)
- USDC (payment currency)

## The 5 Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| /health | GET | Confirms server is alive and returns registered tool count |
| /register | POST | Developer onboards their tool with price, wallet and callback URL |
| /pay/{tool_name} | POST | Buyer pays and triggers a specific registered tool |
| /receipt/{id} | GET | Retrieves tamper-evident proof of payment |
| /status/{id} | GET | Returns current transaction state |

Note: each tool gets its own dedicated route at `/pay/{tool_name}`. This means x402 enforces a separate price per tool. Registering tool A at $0.01 and tool B at $0.50 charges buyers the correct price for each independently.

## How To Run Locally

```bash
git clone https://github.com/l2bg/reqcast.git

cd reqcast

py -3.12 -m venv venv

venv\Scripts\Activate.ps1

pip install -r requirements.txt

uvicorn main:app --reload --port 8000
```

Health check: `http://localhost:8000/health`

Interactive API docs: `http://localhost:8000/docs`

## Environment Variables Required

Create a `.env` file in the root with these values. The server will not start without all of them.

```
REQCAST_WALLET=your_wallet_address_on_base_sepolia
USDC_CONTRACT=0x036CbD53842c5426634e7929541eC2318f3dcf7e
PORT=8000
ENVIRONMENT=testnet
CDP_API_KEY_ID=your_cdp_api_key_id
CDP_API_KEY_SECRET=your_cdp_api_key_secret
CDP_WALLET_SECRET=your_cdp_wallet_secret
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_service_role_key
DEVELOPER_WALLET=test_developer_wallet_address
BUYER_WALLET=test_buyer_wallet_address
BUYER_PRIVATE_KEY=test_buyer_private_key_for_test_script
```

Never commit `.env` to Git. It is blocked by `.gitignore`. Anyone who gets this file can drain the ReqCast wallet.

## Supabase Schema

Run the following SQL in your Supabase project to create the required tables before starting the server.

```sql
CREATE TABLE tools (
    id               bigint generated always as identity primary key,
    tool_name        text unique not null,
    wallet_address   text not null,
    price_per_call   text not null,
    callback_url     text not null,
    timeout_seconds  int not null default 10,
    registered_at    text not null
);

CREATE TABLE transactions (
    id               bigint generated always as identity primary key,
    transaction_id   text unique not null,
    tool_name        text not null,
    status           text not null default 'pending',
    timestamp        text not null,
    price_usdc       float,
    developer_cut    float,
    reqcast_cut      float,
    developer_wallet text,
    payout_tx_hash   text,
    tool_result      jsonb,
    error            text
);
```

## Switching to Mainnet

When moving to mainnet, make two changes only.

In `main.py` change the network string in two places:
```python
# testnet
server.register('eip155:84532', ExactEvmServerScheme())
network='base-sepolia'

# mainnet
server.register('eip155:8453', ExactEvmServerScheme())
network='base'
```

In `.env` change the USDC contract:
```
# testnet
USDC_CONTRACT=0x036CbD53842c5426634e7929541eC2318f3dcf7e

# mainnet
USDC_CONTRACT=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
```

Fund the ReqCast wallet with a small amount of real USDC before opening to external developers. Test one payment manually before going live.

## What Is Proved

- x402 payment rail works on Base Sepolia
- USDC moves from buyer wallet to ReqCast wallet on a single HTTP call
- 5% / 95% split executes automatically on every transaction
- Per-tool x402 pricing works correctly across multiple tools at different prices
- Developer callback URL receives the buyer payload after payment is verified
- Tools and transactions persist in Supabase and survive server restarts
- Receipts are written with full on-chain audit trail including payout tx hash
- Transaction status is queryable by ID

## On-Chain Proof

Both transaction hashes below are real on-chain records on Base Sepolia.

First USDC payment and split mechanic test:
`0xd047aa29a3c1472fddb03d6517730c6dd3f88bc764666ecd4813f35c5f3deee3`

Full architecture test with POST /pay and receipt:
`0xf16a908616af4706dd4a397817b0c9acde86dce0d37ddc2909ac005606ed003b`

Verify at: `https://sepolia.basescan.org/tx/{hash}`

## Status

POC complete. Deployed on Railway. Supabase connected. Ready for first external developer registration.
