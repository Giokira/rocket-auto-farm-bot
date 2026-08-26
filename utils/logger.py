"""Configurazione dei log.

Due destinazioni:
  - console  -> comodo in sviluppo
  - logs/bot.log (rotazione a 2 MB, 5 backup) -> traccia permanente

Il logger "audit" scrive ANCHE su logs/audit.log: contiene solo gli eventi
di business (nuovo ordine, hash ricevuto, conferma, consegna, rifiuto), cioe'
la traccia che serve a ricostruire una vendita contestata.
"""

import logging
from logging.handlers import RotatingFileHandler

import config

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def _file_handler(filename: str, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        config.LOG_DIR / filename,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FMT))
    handler.setLevel(level)
    return handler


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(_file_handler("bot.log", logging.INFO))

    # httpx logga ogni chiamata HTTP a Telegram: troppo rumore a livello INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)

    audit = logging.getLogger("audit")
    audit.setLevel(logging.INFO)
    audit.addHandler(_file_handler("audit.log", logging.INFO))
    # propagate=True: gli eventi audit finiscono anche in bot.log e in console.


audit_log = logging.getLogger("audit")


def log_event(event: str, **fields) -> None:
    """Riga di audit in formato chiave=valore, facile da grep-are.

    Esempio: log_event("ordine_creato", order="ORD-...", user=123, price=9.99)
    """
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    audit_log.info("%s %s", event.upper(), parts)
