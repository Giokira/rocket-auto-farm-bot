"""Consegna al cliente, ramificata per tipo di prodotto.

Isolata dagli handler perche' e' l'unico punto in cui il prodotto esce dal
server: cosi' esiste un solo percorso di consegna da controllare e loggare.

  licenza_file  -> genera la chiave (idempotente), invia .jar + giokiradd.key
                   e le istruzioni. Esito: DELIVERED.
  valuta_ingame -> nessun file: avvisa il compratore e lascia all'admin il
                   compito di pagare in gioco. Esito: PENDING_INGAME.

Regola invariata: prima la consegna riesce, poi il chiamante segna lo stato.
Mai "completato" senza consegna.
"""

import io
import logging
import secrets
from dataclasses import dataclass

from telegram import Bot
from telegram.error import TelegramError

import catalog
import config
import database as db
from keygen import KeygenError, generate_license_key
from utils.logger import log_event

logger = logging.getLogger(__name__)

# Esiti possibili
DELIVERED = "consegnato"          # nulla resta da fare: si puo' completare
PENDING_INGAME = "in_gioco"       # l'admin deve ancora pagare in gioco

KEY_FILENAME = "license.key"


class DeliveryError(Exception):
    """Consegna fallita: l'ordine NON va marcato come completato."""


def issue_online_token() -> str:
    """Token opaco casuale per il modello online. Nessuna firma, nessun segreto:
    la sua validita' e' solo la riga dell'ordine nel database."""
    return config.TOKEN_PREFIX + secrets.token_urlsafe(24)


async def obtain_license_key(mc_username: str) -> str:
    """La chiave da consegnare, secondo il modello configurato.

    online : token casuale (la mod lo valida contro il server licenze).
    offline: firma Ed25519 dal keygen locale (verificata dentro la mod).
    """
    if config.LICENSE_MODE == config.MODE_LICENSE_ONLINE:
        return issue_online_token()
    return await generate_license_key(mc_username)


@dataclass
class DeliveryResult:
    outcome: str
    admin_message: str


async def deliver_order(bot: Bot, order: dict) -> DeliveryResult:
    """Esegue la consegna adatta al tipo di prodotto dell'ordine."""
    product = catalog.get_product(order["product_id"])
    if product is None:
        raise DeliveryError(f"Prodotto '{order['product_id']}' non esiste piu' nel catalogo")

    # product_type dell'ordine (storico) con fallback sul catalogo attuale.
    ptype = order.get("product_type") or product.product_type

    if ptype == catalog.TYPE_LICENSE:
        return await _deliver_license(bot, order, product)
    if ptype == catalog.TYPE_INGAME:
        return await _deliver_ingame(bot, order, product)
    raise DeliveryError(f"Tipo di prodotto sconosciuto: {ptype}")


# --------------------------------------------------------------------------
# Licenza + file
# --------------------------------------------------------------------------

async def _deliver_license(bot: Bot, order: dict, product: catalog.Product) -> DeliveryResult:
    mc_username = (order.get("mc_username") or "").strip()
    if not mc_username:
        raise DeliveryError("Ordine senza username Minecraft: impossibile generare la chiave")

    # Idempotenza: una chiave gia' generata per questo ordine si riusa.
    # Rigenerarla produrrebbe una firma diversa e confonderebbe il cliente.
    license_key = (order.get("license_key") or "").strip()
    if license_key:
        logger.info("Riuso chiave gia' generata per %s", order["order_code"])
    else:
        try:
            license_key = await obtain_license_key(mc_username)
        except KeygenError as exc:
            raise DeliveryError(f"Generazione chiave fallita: {exc}") from exc
        await db.set_license_key(order["order_code"], license_key)
        log_event("chiave_generata", order=order["order_code"], mc_user=mc_username,
                  modello=config.LICENSE_MODE)

    caption = (
        f"Pagamento confermato per l'ordine <b>{order['order_code']}</b>.\n"
        f"<b>{product.name}</b> v{product.version} (Minecraft {product.mc_version})\n"
        f"Licenza intestata a: <b>{mc_username}</b>"
    )

    await _send_product_file(bot, order, product, caption)

    # La chiave come file pronto all'uso...
    key_file = io.BytesIO(license_key.encode("utf-8"))
    try:
        await bot.send_document(
            chat_id=order["user_id"],
            document=key_file,
            filename=KEY_FILENAME,
            caption="La tua chiave di licenza, gia' col nome giusto.",
        )
    except TelegramError as exc:
        raise DeliveryError(f"Invio della chiave fallito: {exc}") from exc

    # ...e come testo copiabile, se il file si perde nella chat.
    online_note = (
        "\n\nLa mod si attiva collegandosi al server: serve <b>connessione a "
        "internet</b> all'avvio. Senza, non parte."
        if config.LICENSE_MODE == config.MODE_LICENSE_ONLINE else ""
    )
    try:
        await bot.send_message(
            chat_id=order["user_id"],
            text=(
                "<b>Come installare</b>\n"
                f"1. Metti il file .jar nella cartella <code>mods</code> dell'istanza.\n"
                f"2. Metti <code>{KEY_FILENAME}</code> nella cartella dell'istanza "
                "(quella che contiene <code>mods</code>).\n"
                "3. Avvia il gioco.\n\n"
                f"La chiave vale solo per lo username <b>{mc_username}</b>."
                f"{online_note}\n\n"
                "Se il file si perde, questa e' la stessa chiave in testo:\n"
                f"<code>{license_key}</code>"
            ),
            parse_mode="HTML",
        )
    except TelegramError as exc:
        raise DeliveryError(f"Invio delle istruzioni fallito: {exc}") from exc

    log_event("consegna_licenza", order=order["order_code"], user=order["user_id"],
              product=product.id, mc_user=mc_username)

    return DeliveryResult(
        DELIVERED,
        f"Licenza consegnata a {mc_username} (ordine {order['order_code']}).",
    )


async def _send_product_file(bot: Bot, order: dict, product: catalog.Product,
                             caption: str) -> None:
    """Invia il .jar, riusando il file_id di Telegram quando possibile."""
    cached_id = await db.get_cached_file_id(product.id)

    if cached_id:
        try:
            await bot.send_document(
                chat_id=order["user_id"],
                document=cached_id,
                caption=caption,
                parse_mode="HTML",
            )
            return
        except TelegramError as exc:
            # file_id scaduto o invalidato: si riparte dal disco.
            logger.warning("file_id in cache non utilizzabile (%s), riprovo da disco", exc)

    path = product.file_path
    if path is None or not path.is_file():
        raise DeliveryError(
            f"File non trovato: {path}. Caricalo nella cartella files/ e riprova."
        )

    try:
        with path.open("rb") as fh:
            message = await bot.send_document(
                chat_id=order["user_id"],
                document=fh,
                filename=product.file,
                caption=caption,
                parse_mode="HTML",
            )
    except TelegramError as exc:
        raise DeliveryError(f"Telegram ha rifiutato l'invio: {exc}") from exc

    if message.document:
        await db.set_cached_file_id(product.id, message.document.file_id)


# --------------------------------------------------------------------------
# Valuta in gioco
# --------------------------------------------------------------------------

async def _deliver_ingame(bot: Bot, order: dict, product: catalog.Product) -> DeliveryResult:
    mc_username = (order.get("mc_username") or "").strip()
    if not mc_username:
        raise DeliveryError("Ordine senza username Minecraft: non so a chi consegnare")

    try:
        await bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"Pagamento confermato per l'ordine <b>{order['order_code']}</b>.\n\n"
                f"<b>{product.name}</b> verra' consegnato in gioco allo username "
                f"<b>{mc_username}</b> a breve.\n"
                "Ricevi un messaggio qui appena la consegna e' fatta."
            ),
            parse_mode="HTML",
        )
    except TelegramError as exc:
        raise DeliveryError(f"Impossibile avvisare il compratore: {exc}") from exc

    log_event("attesa_consegna_ingame", order=order["order_code"], user=order["user_id"],
              product=product.id, mc_user=mc_username)

    return DeliveryResult(
        PENDING_INGAME,
        f"Paga {product.name} in gioco a <b>{mc_username}</b>, poi premi Consegnato.",
    )


async def notify_ingame_delivered(bot: Bot, order: dict) -> None:
    """Messaggio finale al compratore quando l'admin ha pagato in gioco."""
    try:
        await bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"Consegna effettuata per l'ordine <b>{order['order_code']}</b>: "
                f"<b>{order['product_name']}</b> e' stato accreditato in gioco a "
                f"<b>{order.get('mc_username') or '?'}</b>.\n"
                "Grazie per l'acquisto!"
            ),
            parse_mode="HTML",
        )
    except TelegramError as exc:
        raise DeliveryError(f"Impossibile avvisare il compratore: {exc}") from exc

    log_event("consegna_ingame", order=order["order_code"], user=order["user_id"],
              mc_user=order.get("mc_username"))
