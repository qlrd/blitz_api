import asyncio
import logging
from typing import AsyncGenerator, List, Optional

from decouple import config
from fastapi import status
from fastapi.exceptions import HTTPException

from app.models.lightning import (
    Channel,
    FeeRevenue,
    GenericTx,
    InitLnRepoUpdate,
    Invoice,
    LightningInfoLite,
    LnInfo,
    NewAddressInput,
    OnChainTransaction,
    Payment,
    PaymentRequest,
    SendCoinsInput,
    SendCoinsResponse,
)
from app.models.system import APIPlatform
from app.utils import SSE, broadcast_sse_msg, redis_get

PLATFORM = config("platform", cast=str)

ln_node = config("ln_node")
if ln_node == "lnd_grpc":
    import app.repositories.ln_impl.lnd_grpc as ln
elif ln_node == "cln_grpc" and PLATFORM != APIPlatform.RASPIBLITZ:
    import app.repositories.ln_impl.cln_grpc as ln
elif ln_node == "cln_grpc" and PLATFORM == APIPlatform.RASPIBLITZ:
    import app.repositories.ln_impl.specializations.cln_grpc_blitz as ln
elif ln_node == "none":
    logging.info(f"lightning was explicitly turned off")
elif ln_node == "":
    logging.info(f"lightning is not set yet")
else:
    logging.error(f"unknown lightning node: {ln_node}")

GATHER_INFO_INTERVALL = config("gather_ln_info_interval", default=2, cast=float)

_CACHE = {"wallet_balance": None}

ENABLE_FWD_NOTIFICATIONS = config(
    "sse_notify_forward_successes", default=False, cast=bool
)

FWD_GATHER_INTERVAL = config("forwards_gather_interval", default=2.0, cast=float)


if FWD_GATHER_INTERVAL < 0.3:
    raise RuntimeError("forwards_gather_interval cannot be less than 0.3 seconds")


async def initialize_ln_repo() -> AsyncGenerator[InitLnRepoUpdate, None]:
    async for u in ln.initialize_impl():
        yield u


async def get_ln_info_lite() -> LightningInfoLite:
    ln_info = await ln.get_ln_info_impl()
    return LightningInfoLite.from_lninfo(ln_info)


async def get_wallet_balance():
    return await ln.get_wallet_balance_impl()


async def list_all_tx(
    successful_only: bool, index_offset: int, max_tx: int, reversed: bool
) -> List[GenericTx]:
    return await ln.list_all_tx_impl(successful_only, index_offset, max_tx, reversed)


async def list_invoices(
    pending_only: bool, index_offset: int, num_max_invoices: int, reversed: bool
) -> List[Invoice]:
    return await ln.list_invoices_impl(
        pending_only,
        index_offset,
        num_max_invoices,
        reversed,
    )


async def list_on_chain_tx() -> List[OnChainTransaction]:
    return await ln.list_on_chain_tx_impl()


async def list_payments(
    include_incomplete: bool, index_offset: int, max_payments: int, reversed: bool
) -> List[Payment]:
    return await ln.list_payments_impl(
        include_incomplete, index_offset, max_payments, reversed
    )


async def add_invoice(
    value_msat: int, memo: str = "", expiry: int = 3600, is_keysend: bool = False
) -> Invoice:
    return await ln.add_invoice_impl(memo, value_msat, expiry, is_keysend)


async def decode_pay_request(pay_req: str) -> PaymentRequest:
    return await ln.decode_pay_request_impl(pay_req)


async def new_address(input: NewAddressInput) -> str:
    return await ln.new_address_impl(input)


async def send_coins(input: SendCoinsInput) -> SendCoinsResponse:
    res = await ln.send_coins_impl(input)
    _schedule_wallet_balance_update()
    return res


async def send_payment(
    pay_req: str,
    timeout_seconds: int,
    fee_limit_msat: int,
    amount_msat: Optional[int] = None,
) -> Payment:
    res = await ln.send_payment_impl(
        pay_req, timeout_seconds, fee_limit_msat, amount_msat
    )
    _schedule_wallet_balance_update()
    return res


async def channel_open(
    local_funding_amount: int, node_URI: str, target_confs: int
) -> str:

    if local_funding_amount < 1:
        raise ValueError("funding amount needs to be positive")

    if target_confs < 1:
        raise ValueError("target confs needs to be positive")

    if len(node_URI) == 0:
        raise ValueError("node_URI cant be empty")

    if not "@" in node_URI:
        raise ValueError("node_URI must contain @ with node physical address")

    res = await ln.channel_open_impl(local_funding_amount, node_URI, target_confs)
    return res


async def channel_list() -> List[Channel]:
    res = await ln.channel_list_impl()
    return res


async def channel_close(channel_id: int, force_close: bool) -> str:
    res = await ln.channel_close_impl(channel_id, force_close)
    return res


async def get_ln_info() -> LnInfo:
    ln_info = await ln.get_ln_info_impl()
    if PLATFORM == APIPlatform.RASPIBLITZ:
        ln_info.identity_uri = await redis_get("ln_default_address")
    return ln_info


async def unlock_wallet(password: str) -> bool:
    res = await ln.unlock_wallet_impl(password)
    return res


async def get_fee_revenue() -> FeeRevenue:
    return await ln.get_fee_revenue_impl()


async def register_lightning_listener():
    """
    Registers all lightning listeners

    By calling get_ln_info_impl() once, we ensure that wallet is unlocked.
    Implementation will throw HTTPException with status_code 423_LOCKED if otherwise.
    It is the task of the caller to call register_lightning_listener() again
    """

    try:

        if ln_node == "" or ln_node == "none":
            logging.info(
                "SKIPPING register_lightning_listener -> no lightning configured"
            )
            return

        await ln.get_ln_info_impl()

        loop = asyncio.get_event_loop()
        loop.create_task(_handle_info_listener())
        loop.create_task(_handle_invoice_listener())
        loop.create_task(_handle_forward_event_listener())
    except NotImplementedError as r:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=r.args[0])


async def _handle_info_listener():
    last_info = None
    last_info_lite = None
    while True:
        info = await ln.get_ln_info_impl()

        if last_info != info:
            await broadcast_sse_msg(SSE.LN_INFO, info.dict())
            last_info = info

        info_lite = LightningInfoLite.from_lninfo(info)

        if last_info_lite != info_lite:
            await broadcast_sse_msg(SSE.LN_INFO_LITE, info_lite.dict())
            last_info_lite = info_lite

        await asyncio.sleep(GATHER_INFO_INTERVALL)


async def _handle_invoice_listener():
    async for i in ln.listen_invoices():
        await broadcast_sse_msg(SSE.LN_INVOICE_STATUS, i.dict())
        _schedule_wallet_balance_update()


_fwd_update_scheduled = False
_fwd_successes = []


async def _handle_forward_event_listener():
    async def _schedule_fwd_update():
        global _fwd_update_scheduled
        global _fwd_successes

        _fwd_update_scheduled = True

        await asyncio.sleep(FWD_GATHER_INTERVAL)

        if len(_fwd_successes) > 0:
            l = _fwd_successes
            _fwd_successes = []
            await broadcast_sse_msg(SSE.LN_FORWARD_SUCCESSES, l)

        _schedule_wallet_balance_update()
        rev = await get_fee_revenue()
        await broadcast_sse_msg(SSE.LN_FEE_REVENUE, rev.dict())

        _fwd_update_scheduled = False

    async for i in ln.listen_forward_events():
        if ENABLE_FWD_NOTIFICATIONS:
            _fwd_successes.append(i.dict())

        if not _fwd_update_scheduled:
            loop = asyncio.get_event_loop()
            loop.create_task(_schedule_fwd_update())


_wallet_balance_update_scheduled = False


def _schedule_wallet_balance_update():
    async def _perform_update():
        global _wallet_balance_update_scheduled
        _wallet_balance_update_scheduled = True
        await asyncio.sleep(1.1)
        wb = await ln.get_wallet_balance_impl()
        if _CACHE["wallet_balance"] != wb:
            await broadcast_sse_msg(SSE.WALLET_BALANCE, wb.dict())
            _CACHE["wallet_balance"] = wb

        _wallet_balance_update_scheduled = False

    global _wallet_balance_update_scheduled
    if not _wallet_balance_update_scheduled:
        loop = asyncio.get_event_loop()
        loop.create_task(_perform_update())
