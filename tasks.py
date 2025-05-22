import asyncio

from lnbits.core.models import Payment
from lnbits.core.services import create_invoice, pay_invoice, websocket_updater
from lnbits.tasks import register_invoice_listener
from loguru import logger
from lnbits.core.crud import update_payment_extra
import httpx,json
from .crud import get_tpos
from .models import TPoS
from .helpers import get_pr


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_tpos")

    while True:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


async def on_invoice_paid(payment: Payment) -> None:
    if (
        not payment.extra
        or payment.extra.get("tag") != "tpos"
        or payment.extra.get("tipSplitted")
    ):
        return
    tip_amount = payment.extra.get("tip_amount")

    stripped_payment = {
        "amount": payment.amount,
        "fee": payment.fee,
        "checking_id": payment.checking_id,
        "payment_hash": payment.payment_hash,
        "bolt11": payment.bolt11,
    }

    tpos_id = payment.extra.get("tpos_id")
    assert tpos_id

    tpos = await get_tpos(tpos_id)
    assert tpos
    await send_webhook(payment, tpos)
    if payment.extra.get("lnaddress") and payment.extra["lnaddress"] != "":
        calc_amount = payment.amount - ((payment.amount / 100) * tpos.lnaddress_cut)
        pr = await get_pr(payment.extra.get("lnaddress"), calc_amount / 1000)
        if pr:
            payment.extra["lnaddress"] = ""
            paid_payment = await pay_invoice(
                payment_request=pr,
                wallet_id=payment.wallet_id,
                extra={**payment.extra},
            )
            logger.debug(f"tpos: LNaddress paid cut: {paid_payment.checking_id}")

    await websocket_updater(tpos_id, str(stripped_payment))

    if not tip_amount:
        # no tip amount
        return

    wallet_id = tpos.tip_wallet
    assert wallet_id

    tip_payment = await create_invoice(
        wallet_id=wallet_id,
        amount=int(tip_amount),
        internal=True,
        memo="tpos tip",
    )
    logger.debug(f"tpos: tip invoice created: {payment.payment_hash}")

    paid_payment = await pay_invoice(
        payment_request=tip_payment.bolt11,
        wallet_id=payment.wallet_id,
        extra={**payment.extra, "tipSplitted": True},
    )
    logger.debug(f"tpos: tip invoice paid: {checking_id}")

async def send_webhook(payment: Payment, tpos: TPoS):
    if not tpos.webhook_url:
        return

    async with httpx.AsyncClient() as client:
        try:
            r: httpx.Response = await client.post(
                tpos.webhook_url,
                json={
                    "payment_hash": payment.payment_hash,
                    "payment_request": payment.bolt11,
                    "amount": payment.amount,
                    "comment": payment.extra.get("comment"),
                    "webhook_data": payment.extra.get("webhook_data") or "",
                    "tpos": tpos.id,
                    "body": json.loads(tpos.webhook_body)
                    if tpos.webhook_body
                    else "",
                },
                headers=json.loads(tpos.webhook_headers)
                if tpos.webhook_headers
                else None,
                timeout=40,
            )
            await mark_webhook_sent(
                payment.payment_hash,
                r.status_code,
                r.is_success,
                r.reason_phrase,
                r.text,
            )
        except Exception as exc:
            logger.error(exc)
            await mark_webhook_sent(
                payment.payment_hash, -1, False, "Unexpected Error", str(exc)
            )
async def mark_webhook_sent(
    payment_hash: str, status: int, is_success: bool, reason_phrase="", text=""
) -> None:
    await update_payment_extra(
        payment_hash,
        {
            "wh_status": status,  # keep for backwards compability
            "wh_success": is_success,
            "wh_message": reason_phrase,
            "wh_response": text,
        },
    )
