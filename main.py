# ============================================================
# REQCAST - Mainnet-Ready Build
# ============================================================
# /health             - server status + wallet balance
# /tools              - public tool directory
# /register           - developer onboards their tool
# /pay/{tool_name}    - buyer pays and triggers a specific tool
# /receipt/{id}       - proof of payment
# /status/{id}        - transaction state
# ============================================================

import os
import uuid
import json
import base64
import logging
import httpx
import resend
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from supabase import create_client, Client
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, CreateHeadersAuthProvider
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from cdp import CdpClient
from cdp.evm_client import TransactionRequestEIP1559
from eth_abi import encode
from web3 import Web3

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
logger = logging.getLogger("reqcast")

# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================
load_dotenv()

REQCAST_WALLET     = Web3.to_checksum_address(os.getenv("REQCAST_WALLET"))
USDC_CONTRACT      = Web3.to_checksum_address(os.getenv("USDC_CONTRACT"))
PORT               = int(os.getenv("PORT", 8000))
ENVIRONMENT        = os.getenv("ENVIRONMENT", "testnet")
CDP_API_KEY_ID     = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")
CDP_WALLET_SECRET  = os.getenv("CDP_WALLET_SECRET")
SUPABASE_URL       = os.getenv("SUPABASE_URL")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY")
RESEND_API_KEY     = os.getenv("RESEND_API_KEY")
ALERT_EMAIL        = "leandrogspr@gmail.com"

NETWORK_ID = "eip155:84532" if ENVIRONMENT == "testnet" else "eip155:8453"
NETWORK_NAME = "base-sepolia" if ENVIRONMENT == "testnet" else "base"

# ============================================================
# INITIALIZE SUPABASE
# ============================================================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# SUPABASE LOGGER
# ============================================================
def log(event: str, level: str = "INFO", **kwargs):
    msg = f"event={event} " + " ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    getattr(logger, level.lower())(msg)
    try:
        supabase.table("logs").insert({
            "timestamp":        datetime.utcnow().isoformat(),
            "level":            level,
            "event":            event,
            "network":          ENVIRONMENT,
            "tool_name":        kwargs.get("tool_name"),
            "transaction_id":   kwargs.get("transaction_id"),
            "buyer_wallet":     kwargs.get("buyer_wallet"),
            "developer_wallet": kwargs.get("developer_wallet"),
            "amount_usdc":      kwargs.get("amount_usdc"),
            "tx_hash":          kwargs.get("tx_hash"),
            "error":            kwargs.get("error"),
            "meta":             kwargs.get("meta")
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write log to Supabase: {e}")

# ============================================================
# EMAIL ALERT HELPER
# ============================================================
async def send_alert(subject: str, body: str):
    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from":    "alerts@reqcast.com",
            "to":      ALERT_EMAIL,
            "subject": subject,
            "html":    f"<pre style='font-family:monospace;font-size:14px;'>{body}</pre>"
        })
        logger.info(f"Alert email sent: {subject}")
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")

# ============================================================
# RATE LIMITER
# ============================================================
# In-memory: tracks failure timestamps per tool
# 3 failures within 60 minutes suspends the tool
failure_tracker: dict = defaultdict(list)
RATE_LIMIT_MAX    = 3
RATE_LIMIT_WINDOW = 60

def record_failure(tool_name: str) -> bool:
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=RATE_LIMIT_WINDOW)
    failure_tracker[tool_name] = [
        t for t in failure_tracker[tool_name] if t > cutoff
    ]
    failure_tracker[tool_name].append(now)
    if len(failure_tracker[tool_name]) >= RATE_LIMIT_MAX:
        log("rate_limit_triggered",
            level="WARNING",
            tool_name=tool_name,
            error=f"{RATE_LIMIT_MAX} failures in {RATE_LIMIT_WINDOW} minutes")
        return True
    return False

def is_rate_limited(tool_name: str) -> bool:
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=RATE_LIMIT_WINDOW)
    recent = [t for t in failure_tracker[tool_name] if t > cutoff]
    return len(recent) >= RATE_LIMIT_MAX

# ============================================================
# INITIALIZE APP
# ============================================================
app = FastAPI(title="ReqCast", version="1.0.0")

SERVER_START = datetime.utcnow()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.reqcast.com", "https://reqcast.com", "https://l2bg.github.io"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ============================================================
# INITIALIZE X402
# ============================================================
def _cdp_create_headers():
    from cdp.auth import get_auth_headers, GetAuthHeadersOptions
    def make(method, path):
        return get_auth_headers(GetAuthHeadersOptions(
            api_key_id=CDP_API_KEY_ID,
            api_key_secret=CDP_API_KEY_SECRET,
            request_method=method,
            request_host="api.cdp.coinbase.com",
            request_path=path,
        ))
    return {
        "supported": make("GET",  "/platform/v2/x402/supported"),
        "verify":    make("POST", "/platform/v2/x402/verify"),
        "settle":    make("POST", "/platform/v2/x402/settle"),
    }

facilitator = HTTPFacilitatorClient(FacilitatorConfig(
    url="https://api.cdp.coinbase.com/platform/v2/x402",
    auth_provider=CreateHeadersAuthProvider(_cdp_create_headers)
))
server      = x402ResourceServer(facilitator)
server.register(NETWORK_ID, ExactEvmServerScheme())

# ============================================================
# X402 DYNAMIC ROUTES
# ============================================================
routes = {}

def build_route(tool_name: str, price_per_call: str) -> dict:
    return {
        "accepts": {
            "scheme":  "exact",
            "payTo":   REQCAST_WALLET,
            "price":   f"${price_per_call}",
            "network": NETWORK_ID,
        }
    }

def load_routes_from_db():
    result = supabase.table("tools").select("tool_name, price_per_call").execute()
    for tool in result.data:
        routes[f"POST /pay/{tool['tool_name']}"] = build_route(
            tool["tool_name"], tool["price_per_call"]
        )
    logger.info(f"Loaded {len(result.data)} tool routes from Supabase.")

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
    wallet_address:       str
    tool_name:            str
    price_per_call:       str
    callback_url:         HttpUrl
    timeout_seconds:      int = 10
    callback_auth_header: str | None = None
    callback_auth_value:  str | None = None

class PayRequest(BaseModel):
    buyer_payload: dict

# ============================================================
# EXTRACT BUYER WALLET FROM X-PAYMENT HEADER
# ============================================================
def extract_buyer_wallet(request: Request) -> str | None:
    try:
        payment_header = request.headers.get("X-PAYMENT") or request.headers.get("x-payment")
        if not payment_header:
            return None
        padding = 4 - len(payment_header) % 4
        decoded = base64.b64decode(payment_header + "=" * padding).decode("utf-8")
        payload = json.loads(decoded)
        return payload.get("from") or payload.get("payer") or payload.get("sender")
    except Exception:
        return None

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
            address=REQCAST_WALLET,
            transaction=transaction,
            network=NETWORK_NAME
        )
        return tx_hash

# ============================================================
# WALLET BALANCE HELPER
# ============================================================
async def get_wallet_usdc_balance() -> float | None:
    try:
        w3 = Web3(Web3.HTTPProvider(
            "https://sepolia.base.org" if ENVIRONMENT == "testnet"
            else "https://mainnet.base.org"
        ))
        erc20_abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],
                      "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],
                      "type":"function"}]
        contract = w3.eth.contract(address=USDC_CONTRACT, abi=erc20_abi)
        balance_units = contract.functions.balanceOf(REQCAST_WALLET).call()
        return round(balance_units / 1_000_000, 4)
    except Exception as e:
        logger.warning(f"Could not fetch wallet balance: {e}")
        return None


# ============================================================
# 402 INDEX VERIFICATION
# ============================================================
@app.get("/.well-known/402index-verify.txt")
def verify_402index():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("30a4dbf989786021f8117800a094d87abf9e40406ae36b2b104b3d69992e1cd0")

# ============================================================
# ENDPOINT 1 - HEALTH CHECK
# ============================================================
@app.get("/health")
async def health_check():
    tools = supabase.table("tools")         .select("tool_name", count="exact").execute()

    total = supabase.table("transactions")         .select("transaction_id", count="exact").execute()

    completed = supabase.table("transactions")         .select("transaction_id", count="exact")         .eq("status", "completed").execute()

    failed = supabase.table("transactions")         .select("transaction_id", count="exact")         .eq("status", "failed").execute()

    last = supabase.table("transactions")         .select("timestamp")         .eq("status", "completed")         .order("timestamp", desc=True)         .limit(1).execute()

    total_count     = total.count or 0
    completed_count = completed.count or 0
    failed_count    = failed.count or 0
    success_rate    = round((completed_count / total_count * 100), 1) if total_count > 0 else None

    uptime_seconds  = int((datetime.utcnow() - SERVER_START).total_seconds())
    uptime_hours    = uptime_seconds // 3600
    uptime_minutes  = (uptime_seconds % 3600) // 60
    uptime_str      = f"{uptime_hours}h {uptime_minutes}m"

    balance        = await get_wallet_usdc_balance()
    wallet_warning = (balance is not None and balance < 0.50)
    if wallet_warning:
        log("wallet_low", level="WARNING", error=f"ReqCast wallet balance low: ${balance} USDC")
        await send_alert(
            subject=f"[ReqCast] Wallet balance low: ${balance} USDC",
            body=f"WARNING: ReqCast wallet balance has dropped below the threshold.\n\nCurrent balance: ${balance} USDC\nThreshold: $0.50 USDC\nNetwork: {NETWORK_NAME}\nEnvironment: {ENVIRONMENT}\n\nTop up the ReqCast wallet immediately to prevent payout failures."
        )

    return {
        "status":           "ok",
        "version":          "1.0.0",
        "environment":      ENVIRONMENT,
        "network":          NETWORK_NAME,
        "uptime":           uptime_str,
        "registered_tools": tools.count,
        "transactions": {
            "total":        total_count,
            "completed":    completed_count,
            "failed":       failed_count,
            "success_rate": f"{success_rate}%" if success_rate is not None else "n/a"
        },
        "last_transaction": last.data[0]["timestamp"] if last.data else None,
        "docs":             "https://api.reqcast.com/docs"
    }

# ============================================================
# ENDPOINT 1B - PUBLIC TOOL DIRECTORY
# ============================================================
@app.get("/tools")
def list_tools():
    result = supabase.table("tools")         .select("tool_name, price_per_call, registered_at")         .order("registered_at", desc=False)         .execute()

    tools = [
        {
            "tool_name":      tool["tool_name"],
            "price_per_call": tool["price_per_call"],
            "pay_endpoint":   f"https://api.reqcast.com/pay/{tool['tool_name']}",
            "registered_at":  tool["registered_at"]
        }
        for tool in result.data
    ]

    return {
        "tools": tools,
        "total": len(tools)
    }

# ============================================================
# ENDPOINT 2 - REGISTER A TOOL
# ============================================================
@app.post("/register")
def register_tool(request: RegisterRequest):

    existing = supabase.table("tools")         .select("tool_name")         .eq("tool_name", request.tool_name)         .execute()

    if existing.data:
        raise HTTPException(
            status_code=409,
            detail=f"Tool '{request.tool_name}' is already registered."
        )

    try:
        httpx.get(str(request.callback_url), timeout=5.0)
    except httpx.RequestError:
        raise HTTPException(
            status_code=400,
            detail=f"Callback URL '{request.callback_url}' is not reachable."
        )

    applied_timeout = min(request.timeout_seconds, 30)

    supabase.table("tools").insert({
        "tool_name":            request.tool_name,
        "wallet_address":       request.wallet_address,
        "price_per_call":       request.price_per_call,
        "callback_url":         str(request.callback_url),
        "timeout_seconds":      applied_timeout,
        "callback_auth_header": request.callback_auth_header,
        "callback_auth_value":  request.callback_auth_value,
        "registered_at":        datetime.utcnow().isoformat(),
        "network":              ENVIRONMENT
    }).execute()

    routes[f"POST /pay/{request.tool_name}"] = build_route(
        request.tool_name, request.price_per_call
    )

    log("tool_registered",
        tool_name=request.tool_name,
        developer_wallet=request.wallet_address,
        amount_usdc=float(request.price_per_call),
        meta={"callback_url": str(request.callback_url), "timeout": applied_timeout})

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
# ENDPOINT 3 - PAY AND TRIGGER TOOL
# ============================================================
@app.post("/pay/{tool_name}")
async def pay(tool_name: str, request: PayRequest, raw_request: Request):

    transaction_id = str(uuid.uuid4())
    timestamp      = datetime.utcnow().isoformat()

    # Extract buyer wallet from x402 payment header
    buyer_wallet = extract_buyer_wallet(raw_request)

    # Check rate limit
    if is_rate_limited(tool_name):
        log("rate_limit_blocked",
            level="WARNING",
            tool_name=tool_name,
            transaction_id=transaction_id,
            buyer_wallet=buyer_wallet,
            error="Tool suspended due to repeated failures")
        raise HTTPException(
            status_code=503,
            detail=f"Tool '{tool_name}' is temporarily suspended due to repeated failures."
        )

    # Look up tool
    result = supabase.table("tools")         .select("*")         .eq("tool_name", tool_name)         .execute()

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' is not registered."
        )

    tool = result.data[0]
    price         = float(tool["price_per_call"])
    developer_cut = round(price * 0.95, 6)
    reqcast_cut   = round(price * 0.05, 6)

    # Write pending transaction with buyer wallet and network
    supabase.table("transactions").insert({
        "transaction_id": transaction_id,
        "tool_name":      tool_name,
        "status":         "pending",
        "timestamp":      timestamp,
        "buyer_wallet":   buyer_wallet,
        "network":        ENVIRONMENT
    }).execute()

    log("payment_attempt",
        tool_name=tool_name,
        transaction_id=transaction_id,
        buyer_wallet=buyer_wallet,
        developer_wallet=tool["wallet_address"],
        amount_usdc=price)

    # Fire callback to developer tool
    try:
        callback_headers = {
            "X-ReqCast-Verified": "true"
        }

        if tool.get("callback_auth_header") and tool.get("callback_auth_value"):
            callback_headers[tool["callback_auth_header"]] = tool["callback_auth_value"]

        async with httpx.AsyncClient() as client:
            tool_response = await client.post(
                url=tool["callback_url"],
                json={"input": request.buyer_payload},
                headers=callback_headers,
                timeout=tool["timeout_seconds"]
            )
    except httpx.TimeoutException:
        record_failure(tool_name)

        # Auto-refund if buyer wallet is known
        refund_tx = None
        if buyer_wallet:
            try:
                refund_tx = await send_usdc(buyer_wallet, price)
                log("refund_sent",
                    tool_name=tool_name,
                    transaction_id=transaction_id,
                    buyer_wallet=buyer_wallet,
                    developer_wallet=tool["wallet_address"],
                    amount_usdc=price,
                    tx_hash=refund_tx,
                    error="Tool timed out")
            except Exception as e:
                log("refund_failed",
                    level="ERROR",
                    tool_name=tool_name,
                    transaction_id=transaction_id,
                    buyer_wallet=buyer_wallet,
                    error=str(e))

        supabase.table("transactions")             .update({
                "status":          "refunded" if refund_tx else "failed",
                "error":           "Tool timed out",
                "refund_tx_hash":  refund_tx
            })             .eq("transaction_id", transaction_id)             .execute()

        log("callback_timeout",
            level="ERROR",
            tool_name=tool_name,
            transaction_id=transaction_id,
            buyer_wallet=buyer_wallet,
            developer_wallet=tool["wallet_address"],
            error="Tool timed out")

        raise HTTPException(
            status_code=502,
            detail=f"Tool '{tool_name}' timed out. Refund initiated." if refund_tx
                   else f"Tool '{tool_name}' timed out. Contact support with transaction ID: {transaction_id}"
        )

    except httpx.RequestError as e:
        record_failure(tool_name)

        refund_tx = None
        if buyer_wallet:
            try:
                refund_tx = await send_usdc(buyer_wallet, price)
                log("refund_sent",
                    tool_name=tool_name,
                    transaction_id=transaction_id,
                    buyer_wallet=buyer_wallet,
                    developer_wallet=tool["wallet_address"],
                    amount_usdc=price,
                    tx_hash=refund_tx,
                    error="Tool unreachable")
            except Exception as re:
                log("refund_failed",
                    level="ERROR",
                    tool_name=tool_name,
                    transaction_id=transaction_id,
                    buyer_wallet=buyer_wallet,
                    error=str(re))

        supabase.table("transactions")             .update({
                "status":         "refunded" if refund_tx else "failed",
                "error":          str(e),
                "refund_tx_hash": refund_tx
            })             .eq("transaction_id", transaction_id)             .execute()

        log("callback_unreachable",
            level="ERROR",
            tool_name=tool_name,
            transaction_id=transaction_id,
            buyer_wallet=buyer_wallet,
            developer_wallet=tool["wallet_address"],
            error=str(e))

        raise HTTPException(
            status_code=502,
            detail=f"Tool '{tool_name}' unreachable. Refund initiated." if refund_tx
                   else f"Tool '{tool_name}' unreachable. Contact support with transaction ID: {transaction_id}"
        )

    # Tool succeeded - execute split
    tx_hash = await send_usdc(tool["wallet_address"], developer_cut)

    supabase.table("transactions").update({
        "status":           "completed",
        "price_usdc":       price,
        "developer_cut":    developer_cut,
        "reqcast_cut":      reqcast_cut,
        "developer_wallet": tool["wallet_address"],
        "payout_tx_hash":   tx_hash,
        "tool_result":      tool_response.json()
    }).eq("transaction_id", transaction_id).execute()

    log("payout_success",
        tool_name=tool_name,
        transaction_id=transaction_id,
        buyer_wallet=buyer_wallet,
        developer_wallet=tool["wallet_address"],
        amount_usdc=developer_cut,
        tx_hash=tx_hash)

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
            "reqcast_cut":      reqcast_cut,
            "developer_wallet": tool["wallet_address"],
            "payout_tx_hash":   tx_hash,
            "tool_result":      tool_response.json()
        }
    }

# ============================================================
# ENDPOINT 4 - GET RECEIPT
# ============================================================
@app.get("/receipt/{transaction_id}")
def get_receipt(transaction_id: str):
    result = supabase.table("transactions")         .select("*")         .eq("transaction_id", transaction_id)         .execute()

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"No receipt found for transaction '{transaction_id}'."
        )
    return result.data[0]

# ============================================================
# ENDPOINT 5 - GET TRANSACTION STATUS
# ============================================================
@app.get("/status/{transaction_id}")
def get_status(transaction_id: str):
    result = supabase.table("transactions")         .select("transaction_id, status, timestamp")         .eq("transaction_id", transaction_id)         .execute()

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"Transaction '{transaction_id}' not found."
        )
    return result.data[0]