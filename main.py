# ============================================================
# ZEEMO — Phase 6 — Per-Tool x402 Routing
# ============================================================
# All endpoints live here.
# /health             — server status
# /register           — developer onboards their tool
# /pay/{tool_name}    — buyer pays and triggers a specific tool
# /receipt/{id}       — proof of payment
# /status/{id}        — transaction state
# ============================================================

import os
import uuid
import httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, HttpUrl
from supabase import create_client, Client
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from cdp import CdpClient
from cdp.evm_client import TransactionRequestEIP1559
from eth_abi import encode
from web3 import Web3

# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================
load_dotenv()

ZEEMO_WALLET       = Web3.to_checksum_address(os.getenv("ZEEMO_WALLET"))
USDC_CONTRACT      = Web3.to_checksum_address(os.getenv("USDC_CONTRACT"))
PORT               = int(os.getenv("PORT", 8000))
ENVIRONMENT        = os.getenv("ENVIRONMENT")
CDP_API_KEY_ID     = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")
CDP_WALLET_SECRET  = os.getenv("CDP_WALLET_SECRET")
SUPABASE_URL       = os.getenv("SUPABASE_URL")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY")

# ============================================================
# INITIALIZE SUPABASE
# ============================================================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# INITIALIZE APP
# ============================================================
app = FastAPI(title="Zeemo", version="0.3.0")

# ============================================================
# INITIALIZE X402
# ============================================================
facilitator = HTTPFacilitatorClient()
server      = x402ResourceServer(facilitator)
server.register("eip155:84532", ExactEvmServerScheme())

# ============================================================
# X402 DYNAMIC ROUTES
# ============================================================
# Each tool gets its own route: POST /pay/{tool_name}
# This means x402 enforces a different price per tool.
# Routes are rebuilt from Supabase on every server startup
# so registered tools survive restarts.
# ============================================================
routes = {}

def build_route(tool_name: str, price_per_call: str) -> dict:
    return {
        "accepts": {
            "scheme":  "exact",
            "payTo":   ZEEMO_WALLET,
            "price":   f"${price_per_call}",
            "network": "eip155:84532",
        }
    }

def load_routes_from_db():
    result = supabase.table("tools").select("tool_name, price_per_call").execute()
    for tool in result.data:
        routes[f"POST /pay/{tool['tool_name']}"] = build_route(
            tool["tool_name"], tool["price_per_call"]
        )
    print(f"Loaded {len(result.data)} tool routes from Supabase.")

load_routes_from_db()

# ============================================================
# ATTACH X402 MIDDLEWARE
# ============================================================
@app.middleware("http")
async def x402_middleware(request: Request, call_next):
    return await payment_middleware(routes, server)(request, call_next)

# ============================================================
# REQUEST BODY MODELS
# ============================================================
class RegisterRequest(BaseModel):
    wallet_address:  str
    tool_name:       str
    price_per_call:  str
    callback_url:    HttpUrl
    timeout_seconds: int = 10

class PayRequest(BaseModel):
    buyer_payload: dict  # tool_name comes from URL path, not body

# ============================================================
# USDC TRANSFER HELPER
# ============================================================
async def send_usdc(recipient: str, amount_usdc: float) -> str:
    amount_units = int(amount_usdc * 1_000_000)

    transfer_selector = bytes.fromhex("a9059cbb")
    encoded_params    = encode(
        ["address", "uint256"],
        [Web3.to_checksum_address(recipient), amount_units]
    )
    data = "0x" + (transfer_selector + encoded_params).hex()

    transaction = TransactionRequestEIP1559(
        to=USDC_CONTRACT,
        value=0,
        data=data,
        gas=100000,
    )

    async with CdpClient(
        api_key_id=CDP_API_KEY_ID,
        api_key_secret=CDP_API_KEY_SECRET,
        wallet_secret=CDP_WALLET_SECRET
    ) as cdp:
        tx_hash = await cdp.evm.send_transaction(
            address=ZEEMO_WALLET,
            transaction=transaction,
            network="base-sepolia"
        )
        return tx_hash

# ============================================================
# ENDPOINT 1 — HEALTH CHECK
# ============================================================
@app.get("/health")
def health_check():
    result = supabase.table("tools").select("tool_name", count="exact").execute()
    return {
        "status":           "ok",
        "environment":      ENVIRONMENT,
        "registered_tools": result.count
    }

# ============================================================
# ENDPOINT 2 — REGISTER A TOOL
# ============================================================
@app.post("/register")
def register_tool(request: RegisterRequest):

    # Reject duplicate tool names
    existing = supabase.table("tools") \
        .select("tool_name") \
        .eq("tool_name", request.tool_name) \
        .execute()

    if existing.data:
        raise HTTPException(
            status_code=409,
            detail=f"Tool '{request.tool_name}' is already registered."
        )

    # Validate callback URL is reachable
    try:
        httpx.get(str(request.callback_url), timeout=5.0)
    except httpx.RequestError:
        raise HTTPException(
            status_code=400,
            detail=f"Callback URL '{request.callback_url}' is not reachable."
        )

    applied_timeout = min(request.timeout_seconds, 30)

    # Persist to Supabase
    supabase.table("tools").insert({
        "tool_name":       request.tool_name,
        "wallet_address":  request.wallet_address,
        "price_per_call":  request.price_per_call,
        "callback_url":    str(request.callback_url),
        "timeout_seconds": applied_timeout,
        "registered_at":   datetime.utcnow().isoformat()
    }).execute()

    # Register per-tool x402 route in memory immediately
    routes[f"POST /pay/{request.tool_name}"] = build_route(
        request.tool_name, request.price_per_call
    )

    return {
        "status":          "registered",
        "tool_name":       request.tool_name,
        "price_per_call":  request.price_per_call,
        "callback_url":    str(request.callback_url),
        "timeout_seconds": applied_timeout,
        "pay_endpoint":    f"/pay/{request.tool_name}",
        "warning":         ("Timeout capped at 30s." if request.timeout_seconds > 30 else None)
    }

# ============================================================
# ENDPOINT 3 — PAY AND TRIGGER TOOL
# ============================================================
# tool_name is in the URL path — each tool has its own
# x402-protected route with its own price. Bug fixed.
# ============================================================
@app.post("/pay/{tool_name}")
async def pay(tool_name: str, request: PayRequest):

    transaction_id = str(uuid.uuid4())
    timestamp      = datetime.utcnow().isoformat()

    # Look up tool from Supabase
    result = supabase.table("tools") \
        .select("*") \
        .eq("tool_name", tool_name) \
        .execute()

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' is not registered."
        )

    tool = result.data[0]

    # Write pending transaction
    supabase.table("transactions").insert({
        "transaction_id": transaction_id,
        "tool_name":      tool_name,
        "status":         "pending",
        "timestamp":      timestamp
    }).execute()

    # Fire callback to developer tool
    try:
        async with httpx.AsyncClient() as client:
            tool_response = await client.post(
                url=tool["callback_url"],
                json={"input": request.buyer_payload},
                headers={"X-Zeemo-Verified": "true"},
                timeout=tool["timeout_seconds"]
            )
    except httpx.TimeoutException:
        supabase.table("transactions") \
            .update({"status": "failed", "error": "Tool timed out"}) \
            .eq("transaction_id", transaction_id) \
            .execute()
        raise HTTPException(
            status_code=502,
            detail=f"Tool '{tool_name}' timed out. Payment not charged."
        )
    except httpx.RequestError as e:
        supabase.table("transactions") \
            .update({"status": "failed", "error": str(e)}) \
            .eq("transaction_id", transaction_id) \
            .execute()
        raise HTTPException(
            status_code=502,
            detail=f"Tool '{tool_name}' unreachable. Payment not charged."
        )

    # Execute split
    price         = float(tool["price_per_call"])
    developer_cut = round(price * 0.95, 6)
    zeemo_cut     = round(price * 0.05, 6)

    tx_hash = await send_usdc(tool["wallet_address"], developer_cut)

    # Write completed receipt
    supabase.table("transactions").update({
        "status":           "completed",
        "price_usdc":       price,
        "developer_cut":    developer_cut,
        "zeemo_cut":        zeemo_cut,
        "developer_wallet": tool["wallet_address"],
        "payout_tx_hash":   tx_hash,
        "tool_result":      tool_response.json()
    }).eq("transaction_id", transaction_id).execute()

    print(f"Transaction {transaction_id} completed. Payout: {tx_hash}")

    return {
        "transaction_id": transaction_id,
        "result":         tool_response.json(),
        "receipt": {
            "transaction_id":   transaction_id,
            "tool_name":        tool_name,
            "status":           "completed",
            "timestamp":        timestamp,
            "price_usdc":       price,
            "developer_cut":    developer_cut,
            "zeemo_cut":        zeemo_cut,
            "developer_wallet": tool["wallet_address"],
            "payout_tx_hash":   tx_hash,
            "tool_result":      tool_response.json()
        }
    }

# ============================================================
# ENDPOINT 4 — GET RECEIPT
# ============================================================
@app.get("/receipt/{transaction_id}")
def get_receipt(transaction_id: str):
    result = supabase.table("transactions") \
        .select("*") \
        .eq("transaction_id", transaction_id) \
        .execute()

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"No receipt found for transaction '{transaction_id}'."
        )
    return result.data[0]

# ============================================================
# ENDPOINT 5 — GET TRANSACTION STATUS
# ============================================================
@app.get("/status/{transaction_id}")
def get_status(transaction_id: str):
    result = supabase.table("transactions") \
        .select("transaction_id, status, timestamp") \
        .eq("transaction_id", transaction_id) \
        .execute()

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"Transaction '{transaction_id}' not found."
        )
    return result.data[0]
