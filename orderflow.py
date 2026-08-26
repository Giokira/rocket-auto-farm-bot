"""Transizioni di stato che coinvolgono la consegna.

Sta fuori dagli handler perche' la conferma di un ordine puo' partire da due
posti: il bottone dell'admin e (solo in modalita' Beni e servizi con verifica
API riuscita) l'auto-conferma. Un'unica funzione garantisce che la regola
"prima la consegna, poi lo stato" valga in entrambi i casi.
"""

import logging

from telegram import Bot
from telegram.error import TelegramError

import database as db
from delivery import (
    DELIVERED,
    PENDING_INGAME,
    DeliveryError,
    deliver_order,
    notify_ingame_delivered,
)
from utils.logger import log_event

logger = logging.getLogger(__name__)


async def confirm_and_deliver(bot: Bot, order: dict, actor_id: int,
                              note: str | None = None) -> tuple[bool, str]:
    """Conferma il pagamento ed esegue la consegna adatta al tipo.

    Ritorna (ok, messaggio_per_admin). In caso di errore l'ordine resta nello
    stato precedente, cosi' si puo' ritentare dopo aver risolto il problema.
    """
    if order["status"] != db.STATUS_VERIFYING:
        return False, f"Stato non confermabile: {db.STATUS_LABELS.get(order['status'])}"

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
                  pagamento=order.get("paypal_txn_id"), admin=actor_id, esito="completato")
        return True, result.admin_message

    if result.outcome == PENDING_INGAME:
        await db.set_status(
            order["order_code"], db.STATUS_TO_DELIVER_INGAME,
            admin_note=note or "Pagamento confermato, consegna in gioco da fare",
        )
        log_event("ordine_confermato", order=order["order_code"], user=order["user_id"],
                  product=order["product_id"], price=order["price"],
                  pagamento=order.get("paypal_txn_id"), admin=actor_id,
                  esito="da_consegnare_in_gioco")
        return True, result.admin_message

    return False, f"Esito di consegna sconosciuto: {result.outcome}"


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
