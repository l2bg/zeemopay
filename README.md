\# Zeemo



Payment infrastructure for AI tools. Zeemo sits between buyers and paid 

AI tools. Every transaction passes through Zeemo. Zeemo verifies the payment, 

triggers the tool, writes a receipt, takes a 5% cut, and pays the developer 

95% instantly.



\## What This Repo Contains



This is the complete proof of concept. All five endpoints live in `main.py`.

The payment rail is x402 USDC on Base L2 (Coinbase). The split mechanic is 

proved and working on Base Sepolia testnet.



\## Stack



\- Python 3.12

\- FastAPI + Uvicorn

\- x402 v2.3.0 — payment middleware

\- Coinbase CDP SDK — wallet and transaction management

\- Base Sepolia — testnet (eip155:84532)

\- USDC — payment currency



\## The 5 Endpoints



| Endpoint | Method | Purpose |

|----------|--------|---------|

| /health | GET | Confirms server is alive |

| /register | POST | Developer onboards their tool |

| /pay | POST | Buyer pays and triggers tool |

| /receipt/{id} | GET | Retrieves proof of payment |

| /status/{id} | GET | Returns transaction state |



\## How To Run Locally



1\. Clone the repo

2\. Create a Python 3.12 virtual environment

3\. Install dependencies

4\. Create a .env file with your credentials

5\. Start the server

```bash

git clone https://github.com/l2bg/zeemopay.git

cd zeemopay

py -3.12 -m venv venv

venv\\Scripts\\Activate.ps1

pip install -r requirements.txt

uvicorn main:app --reload --port 8000

```



\## Environment Variables Required



Create a `.env` file in the root with these values:

```

ZEEMO\_WALLET=your\_zeemo\_wallet\_address

USDC\_CONTRACT=0x036CbD53842c5426634e7929541eC2318f3dcf7e

PORT=8000

ENVIRONMENT=testnet

CDP\_API\_KEY\_ID=your\_cdp\_api\_key\_id

CDP\_API\_KEY\_SECRET=your\_cdp\_api\_key\_secret

CDP\_WALLET\_SECRET=your\_cdp\_wallet\_secret

```



\## What Is Proved



\- x402 payment rail works on Base Sepolia

\- USDC moves from buyer wallet to Zeemo wallet on a single HTTP call

\- 5%/95% split executes automatically on every transaction

\- Developer callback URL receives the buyer payload

\- Receipts are written with full audit trail

\- Transaction status is queryable by ID



\## What Comes Next



1\. Deploy to Railway — server runs 24/7 with a real URL

2\. Add Supabase — persistent storage for tools and receipts

3\. Secure secrets — move credentials to environment variable manager

4\. Find first real developer — register a real tool

5\. Switch to mainnet — change network from base-sepolia to base



\## Proof of Concept Status

