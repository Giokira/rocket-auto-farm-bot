"""Transizioni di stato che coinvolgono la consegna.

Sta fuori dagli handler perche' la conferma di un ordine parte dal bottone
dell'admin. Un'unica funzione garantisce che la regola "prima la consegna,
poi lo stato" valga in ogni percorso.

Per le licenze/mod la consegna NON e' automatica alla conferma del pagamento:
l'ordine passa in stato "pagato" e aspetta che il cliente prema "Richiedi mod
e licenza". Solo allora l'admin vede una richiesta con Accetta/Rifiuta.
"""

import logging

from telegram import Bot
from telegram.error import TelegramError

import catalog
import database as db
from delivery import (
    DELIVERED,
    PENDING_INGAME,
    DeliveryError,
    deliver_order,
    notify_ingame_delivered,
)
from handlers.common import esc, request_delivery_keyboard
from utils.logger import log_event

logger = logging.getLogger(__name__)


def _product_type(order: dict) -> str | None:
    product = catalog.get_product(order["product_id"])
    return order.get("product_type") or (product.product_type if product else None)


async def confirm_and_deliver(bot: Bot, order: dict, actor_id: int,
                              note: str | None = None) -> tuple[bool, str]:
    """Conferma il pagamento.

    Valuta in gioco: consegna subito (nessuna richiesta necessaria).
    Licenza/mod: NON consegna. Passa l'ordine in "pagato" e aspetta che il
    cliente la richieda esplicitamente (vedi approve_delivery_request).
    """
    if order["status"] != db.STATUS_VERIFYING:
        return False, f"Stato non confermabile: {db.STATUS_LABELS.get(order['status'])}"

    if _product_type(order) == catalog.TYPE_LICENSE:
        await db.set_status(
            order["order_code"], db.STATUS_PAID,
            admin_note=note or "Pagamento confermato, in attesa di richiesta",
        )
        log_event("ordine_confermato", order=order["order_code"], user=order["user_id"],
                  product=order["product_id"], price=order["price"], admin=actor_id,
                  esito="pagato_in_attesa_richiesta")
        try:
            await bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"Pagamento confermato per l'ordine <b>{esc(order['order_code'])}</b>.\n\n"
                    "Quando vuoi ricevere mod e licenza premi il bottone qui sotto."
                ),
                parse_mode="HTML",
                reply_markup=request_delivery_keyboard(order["order_code"]),
            )
        except TelegramError as exc:
            logger.warning("Impossibile avvisare il compratore di %s: %s",
                           order["order_code"], exc)
        return True, (
            f"Pagamento confermato per {order['order_code']}. "
            "In attesa che il cliente richieda mod e licenza."
        )

    try:
        result = await deliver_order(bot, order)
    except DeliveryError as exc:
        logger.error("Consegna fallita per %s: %s", order["order_code"], exc)
        log_event("consegna_fallita", order=order["order_code"],
                  user=order["user_id"], errore=str(exc))
        return False, (
            f"Consegna NON riuscita: {exc}\n"
            "L'ordine resta in verifica: risolvi e premi di nuovo Conferma."
        )
    except TelegramError as exc:
        # Tipicamente: l'utente ha bloccato il bot.
        logger.error("Telegram ha rifiutato la consegna di %s: %s", order["order_code"], exc)
        log_event("consegna_fallita", order=order["order_code"],
                  user=order["user_id"], errore=str(exc))
        return False, (
            f"Consegna NON riuscita (Telegram: {exc}).\n"
            "Probabilmente l'utente ha bloccato il bot. Ordine lasciato in verifica."
        )

    if result.outcome == DELIVERED:
        await db.set_status(
            order["order_code"], db.STATUS_COMPLETED,
            admin_note=note or "Confermato manualmente", mark_delivered=True,
        )
        log_event("ordine_confermato", order=order["order_code"], user=order["user_id"],
                  product=order["product_id"], price=order["price"],
                  admin=actor_id, esito="completato")
        return True, result.admin_message

    if result.outcome == PENDING_INGAME:
        await db.set_status(
            order["order_code"], db.STATUS_TO_DELIVER_INGAME,
            admin_note=note or "Pagamento confermato, consegna in gioco da fare",
        )
        log_event("ordine_confermato", order=order["order_code"], user=order["user_id"],
                  product=order["product_id"], price=order["price"],
                  admin=actor_id, esito="da_consegnare_in_gioco")
        return True, result.admin_message

    return False, f"Esito di consegna sconosciuto: {result.outcome}"


async def request_delivery(order: dict) -> tuple[bool, str]:
    """Il cliente chiede di ricevere mod e licenza. Ritorna (ok, motivo)."""
    if order["status"] != db.STATUS_PAID:
        return False, f"Stato non valido: {db.STATUS_LABELS.get(order['status'])}"
    await db.set_status(order["order_code"], db.STATUS_DELIVERY_REQUESTED,
                        admin_note="Richiesta mod/licenza dal cliente")
    log_event("richiesta_consegna", order=order["order_code"], user=order["user_id"])
    return True, "ok"


async def approve_delivery_request(bot: Bot, order: dict, actor_id: int) -> tuple[bool, str]:
    """L'admin accetta la richiesta: consegna mod e licenza."""
    if order["status"] != db.STATUS_DELIVERY_REQUESTED:
        return False, f"Stato non valido: {db.STATUS_LABELS.get(order['status'])}"

    try:
        result = await deliver_order(bot, order)
    except DeliveryError as exc:
        logger.error("Consegna fallita per %s: %s", order["order_code"], exc)
        log_event("consegna_fallita", order=order["order_code"],
                  user=order["user_id"], errore=str(exc))
        return False, (
            f"Consegna NON riuscita: {exc}\n"
            "L'ordine resta in attesa di richiesta: risolvi e riprova."
        )
    except TelegramError as exc:
        logger.error("Telegram ha rifiutato la consegna di %s: %s", order["order_code"], exc)
        log_event("consegna_fallita", order=order["order_code"],
                  user=order["user_id"], errore=str(exc))
        return False, (
            f"Consegna NON riuscita (Telegram: {exc}).\n"
            "Probabilmente l'utente ha bloccato il bot."
        )

    if result.outcome != DELIVERED:
        return False, f"Esito di consegna sconosciuto: {result.outcome}"

    await db.set_status(order["order_code"], db.STATUS_COMPLETED,
                        admin_note="Consegna approvata dall'admin", mark_delivered=True)
    log_event("richiesta_consegna_approvata", order=order["order_code"],
              user=order["user_id"], admin=actor_id)
    return True, result.admin_message


async def reject_delivery_request(bot: Bot, order: dict, actor_id: int) -> tuple[bool, str]:
    """L'admin rifiuta la richiesta: l'ordine torna 'pagato', il cliente puo' richiedere di nuovo."""
    if order["status"] != db.STATUS_DELIVERY_REQUESTED:
        return False, f"Stato non valido: {db.STATUS_LABELS.get(order['status'])}"

    await db.set_status(order["order_code"], db.STATUS_PAID,
                        admin_note="Richiesta mod/licenza rifiutata dall'admin")
    log_event("richiesta_consegna_rifiutata", order=order["order_code"],
              user=order["user_id"], admin=actor_id)

    try:
        await bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"La richiesta di mod e licenza per l'ordine <b>{esc(order['order_code'])}</b> "
                "non e' stata accettata. Scrivimi se pensi ci sia un errore, oppure "
                "riprova piu' tardi."
            ),
            parse_mode="HTML",
        )
    except TelegramError as exc:
        logger.warning("Impossibile avvisare il compratore di %s: %s",
                       order["order_code"], exc)

    return True, f"Richiesta rifiutata per {order['order_code']}."


async def mark_ingame_delivered(bot: Bot, order: dict, actor_id: int) -> tuple[bool, str]:
    """Chiude un ordine di valuta dopo che l'admin ha pagato in gioco."""
    if order["status"] != db.STATUS_TO_DELIVER_INGAME:
        return False, f"Stato inatteso: {db.STATUS_LABELS.get(order['status'])}"

    try:
        await notify_ingame_delivered(bot, order)
    except DeliveryError as exc:
        return False, f"Ordine non chiuso: {exc}"

    await db.set_status(order["order_code"], db.STATUS_COMPLETED,
                        admin_note="Valuta consegnata in gioco", mark_delivered=True)
    log_event("consegna_confermata_ingame", order=order["order_code"],
              user=order["user_id"], mc_user=order.get("mc_username"), admin=actor_id)
    return True, f"Ordine {order['order_code']} completato."
