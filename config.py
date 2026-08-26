"""Caricamento e validazione della configurazione.

Tutti i segreti arrivano da variabili d'ambiente (file .env, mai committato).
Nessun valore sensibile e' hardcoded in questo file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# override=False: le variabili gia' presenti nell'ambiente (es. Docker, systemd)
# hanno la precedenza sul file .env locale.
load_dotenv(BASE_DIR / ".env", override=False)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"La variabile {name} deve essere un numero intero, trovato: {raw!r}")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "on"}


def _path_env(name: str, default: str) -> Path:
    """Percorso relativo alla root del progetto, se non assoluto."""
    raw = (os.getenv(name) or default).strip()
    p = Path(raw)
    return p if p.is_absolute() else BASE_DIR / p


# ---- Telegram (obbligatorie) ----
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID: int = _int_env("ADMIN_USER_ID", 0)

# ---- PayPal ----
PAYPAL_EMAIL: str = os.getenv("PAYPAL_EMAIL", "").strip()
PAYPAL_ME_LINK: str = os.getenv("PAYPAL_ME_LINK", "").strip()

MODE_FRIENDS = "amici_famiglia"
MODE_GOODS = "beni_servizi"
# Default deciso: Amici e Famiglia. Vedi la nota in README sui limiti di questa
# modalita' (nessuna protezione acquisti, contraria alle regole PayPal per la
# vendita di beni, verifica solo manuale).
PAYPAL_MODE: str = (os.getenv("PAYPAL_MODE") or MODE_FRIENDS).strip().lower()

# Verifica automatica: possibile solo per i pagamenti Beni e servizi, perche'
# l'API Orders/Checkout non espone i pagamenti personali (Amici e Famiglia).
PAYPAL_CLIENT_ID: str = os.getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_SECRET: str = os.getenv("PAYPAL_SECRET", "").strip()
PAYPAL_API_BASE: str = (
    os.getenv("PAYPAL_API_BASE") or "https://api-m.paypal.com"
).strip().rstrip("/")
PAYPAL_AUTOCONFIRM: bool = _bool_env("PAYPAL_AUTOCONFIRM", False)
PAYPAL_MERCHANT_ID: str = os.getenv("PAYPAL_MERCHANT_ID", "").strip()

# ---- Metodo di pagamento principale ----
# giftcard: il compratore manda un CODICE (buono regalo Amazon); l'admin lo
#           riscatta e conferma. Nessun nome mostrato al compratore.
# paypal:   pagamento PayPal (mostra il tuo nome, KYC), gift card come ripiego.
METHOD_GIFTCARD = "giftcard"
METHOD_PAYPAL = "paypal"
PAYMENT_METHOD: str = (os.getenv("PAYMENT_METHOD") or METHOD_GIFTCARD).strip().lower()

# ---- Gift card / buono a codice ----
GIFTCARD_ENABLED: bool = _bool_env("GIFTCARD_ENABLED", True)
GIFTCARD_INSTRUCTIONS: str = os.getenv("GIFTCARD_INSTRUCTIONS", "").strip()


def giftcard_mode() -> bool:
    """True quando il buono a codice e' il metodo principale."""
    return PAYMENT_METHOD == METHOD_GIFTCARD

# ---- Modello di licenza ----
# online : la "chiave" e' un token opaco casuale; la mod la valida contro il
#          server licenze e scarica da li' la logica. Nessuna chiave privata da
#          custodire. E' il modello forte, adatto al VPS.
# offline: la chiave e' una firma Ed25519 generata dal keygen locale, verificata
#          dentro la mod. Richiede la chiave privata sulla macchina.
MODE_LICENSE_ONLINE = "online"
MODE_LICENSE_OFFLINE = "offline"
LICENSE_MODE: str = (os.getenv("LICENSE_MODE") or MODE_LICENSE_ONLINE).strip().lower()

# Prefisso leggibile dei token online. Non ha alcun ruolo di sicurezza.
TOKEN_PREFIX: str = os.getenv("TOKEN_PREFIX", "GKR1-").strip()

# ---- Generazione chiavi di licenza (solo modello offline) ----
# {username} viene sostituito con lo username Minecraft del compratore.
KEYGEN_CMD: str = (os.getenv("KEYGEN_CMD") or "java tools/Keygen.java {username}").strip()
PRIVATE_KEY_PATH: Path = _path_env("PRIVATE_KEY_PATH", "tools/giokiradd-private.key")
KEYGEN_TIMEOUT: int = _int_env("KEYGEN_TIMEOUT", 60)
KEYGEN_CWD: Path = _path_env("KEYGEN_CWD", ".")

# ---- Server di validazione licenze (lato VPS) ----
# Ascolta su localhost: il TLS lo mette un reverse proxy davanti. Il payload e'
# il file con le classi protette della mod, servito solo dopo un token valido.
LICENSE_HOST: str = os.getenv("LICENSE_HOST", "127.0.0.1").strip()
LICENSE_PORT: int = _int_env("LICENSE_PORT", 8787)
PAYLOAD_PATH: Path = _path_env("PAYLOAD_PATH", "payload/rocket-auto-farm.bin")

# ---- Percorsi e log ----
DB_PATH: Path = _path_env("DB_PATH", "orders.db")
FILES_DIR: Path = _path_env("FILES_DIR", "files")
LOG_DIR: Path = BASE_DIR / "logs"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

ORDER_RATE_LIMIT_MAX: int = _int_env("ORDER_RATE_LIMIT_MAX", 3)
ORDER_RATE_LIMIT_SECONDS: int = _int_env("ORDER_RATE_LIMIT_SECONDS", 60)


def paypal_auto_verify_enabled() -> bool:
    """True solo se la verifica automatica ha senso ed e' configurata.

    In modalita' Amici e Famiglia la verifica API e' disattivata per scelta:
    i pagamenti personali non compaiono nelle Orders API, quindi ogni chiamata
    darebbe un falso negativo. Resta la verifica manuale dell'admin.
    """
    return (
        PAYPAL_MODE == MODE_GOODS
        and bool(PAYPAL_CLIENT_ID)
        and bool(PAYPAL_SECRET)
    )


def paypal_destination() -> str:
    """Cosa mostrare al compratore per pagare."""
    return PAYPAL_ME_LINK or PAYPAL_EMAIL


def validate() -> list[str]:
    """Blocca l'avvio se manca l'essenziale; ritorna gli avvisi non bloccanti."""
    missing = []
    if not BOT_TOKEN or "PLACEHOLDER" in BOT_TOKEN.upper():
        missing.append("TELEGRAM_BOT_TOKEN")
    if ADMIN_USER_ID <= 0:
        missing.append("ADMIN_USER_ID")
    if PAYMENT_METHOD == METHOD_PAYPAL and not PAYPAL_EMAIL and not PAYPAL_ME_LINK:
        missing.append("PAYPAL_EMAIL (oppure PAYPAL_ME_LINK)")

    if missing:
        raise RuntimeError(
            "Configurazione incompleta. Variabili mancanti o non compilate: "
            + ", ".join(missing)
            + "\nCopia .env.example in .env e inserisci i valori reali."
        )

    if PAYPAL_MODE not in (MODE_FRIENDS, MODE_GOODS):
        raise RuntimeError(
            f"PAYPAL_MODE non valido: {PAYPAL_MODE!r}. "
            f"Valori ammessi: {MODE_FRIENDS!r} o {MODE_GOODS!r}."
        )

    if PAYMENT_METHOD not in (METHOD_GIFTCARD, METHOD_PAYPAL):
        raise RuntimeError(
            f"PAYMENT_METHOD non valido: {PAYMENT_METHOD!r}. "
            f"Valori ammessi: {METHOD_GIFTCARD!r} o {METHOD_PAYPAL!r}."
        )

    warnings = []
    if PAYPAL_MODE == MODE_FRIENDS:
        warnings.append(
            "PAYPAL_MODE=amici_famiglia: i pagamenti personali PayPal non sono "
            "ammessi per la vendita di beni e non danno protezione ne' a te ne' "
            "al compratore. PayPal puo' limitare il conto. La verifica automatica "
            "e' disattivata: ogni pagamento va controllato a mano."
        )
    if PAYPAL_AUTOCONFIRM and not paypal_auto_verify_enabled():
        warnings.append(
            "PAYPAL_AUTOCONFIRM=true ma la verifica automatica non e' attiva "
            "(serve PAYPAL_MODE=beni_servizi con CLIENT_ID e SECRET). "
            "Le conferme restano manuali."
        )
    if LICENSE_MODE not in (MODE_LICENSE_ONLINE, MODE_LICENSE_OFFLINE):
        raise RuntimeError(
            f"LICENSE_MODE non valido: {LICENSE_MODE!r}. "
            f"Valori ammessi: {MODE_LICENSE_ONLINE!r} o {MODE_LICENSE_OFFLINE!r}."
        )

    if LICENSE_MODE == MODE_LICENSE_OFFLINE and PRIVATE_KEY_PATH.is_file():
        warnings.append(
            f"Modello offline con chiave privata in {PRIVATE_KEY_PATH}. Va bene "
            "solo su macchina fidata: su un VPS chi la ruba conia licenze infinite. "
            "Sul VPS usa LICENSE_MODE=online."
        )
    if LICENSE_MODE == MODE_LICENSE_ONLINE and PRIVATE_KEY_PATH.is_file():
        warnings.append(
            f"Modello online ma la chiave privata e' ancora in {PRIVATE_KEY_PATH}: "
            "in online non serve, rimuovila dalla macchina."
        )

    FILES_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return warnings
