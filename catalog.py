"""Catalogo prodotti.

Definito in codice (non su DB) perche' i prodotti sono pochi e cambiano di rado:
per cambiare prezzo o nome si tocca solo questo file, mai il database.

Due tipi di prodotto, con consegna diversa:

  TYPE_LICENSE  ("licenza_file")  -> si consegna il .jar + una chiave di licenza
                                     generata al momento e legata allo username
                                     Minecraft del compratore.
  TYPE_INGAME   ("valuta_ingame") -> nessun file: l'admin paga la valuta in gioco
                                     allo username indicato e poi conferma.

Campi:
  file       -> nome del file dentro FILES_DIR (solo per TYPE_LICENSE).
  screenshot -> immagine dentro FILES_DIR (opzionale, None per nessuna).
"""

from dataclasses import dataclass
from pathlib import Path

import config

TYPE_LICENSE = "licenza_file"
TYPE_INGAME = "valuta_ingame"

TYPE_LABELS = {
    TYPE_LICENSE: "Licenza + file",
    TYPE_INGAME: "Valuta in gioco",
}


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    product_type: str
    description: str
    price: float
    currency: str = "EUR"
    file: str | None = None
    screenshot: str | None = None
    version: str = "1.0.0"
    mc_version: str = "1.20.1"

    @property
    def file_path(self) -> Path | None:
        return config.FILES_DIR / self.file if self.file else None

    @property
    def screenshot_path(self) -> Path | None:
        return config.FILES_DIR / self.screenshot if self.screenshot else None

    @property
    def price_label(self) -> str:
        # Formato italiano: 10,00 EUR
        return f"{self.price:.2f}".replace(".", ",") + f" {self.currency}"

    @property
    def needs_file(self) -> bool:
        return self.product_type == TYPE_LICENSE

    @property
    def needs_license_key(self) -> bool:
        return self.product_type == TYPE_LICENSE

    def is_available(self) -> tuple[bool, str]:
        """Il prodotto puo' essere venduto adesso? (ok, motivo tecnico)."""
        if self.needs_file:
            path = self.file_path
            if path is None or not path.is_file():
                return False, f"file mancante: {path}"
        return True, ""


PRODUCTS: dict[str, Product] = {
    "licenza_mod": Product(
        id="licenza_mod",
        name="Licenza mod",
        product_type=TYPE_LICENSE,
        description=(
            "Licenza personale per la mod.\n"
            "Include il file .jar e una chiave legata al tuo username Minecraft.\n"
            "La chiave funziona SOLO con lo username che indichi durante l'ordine."
        ),
        price=10.00,
        file="rocket-auto-farm.jar",
        screenshot=None,
        version="0.1.0",
        mc_version="26.1.2",
    ),
    "ingame_1m": Product(
        id="ingame_1m",
        name="1M in-game",
        product_type=TYPE_INGAME,
        description="1 milione di valuta in gioco, consegnata a mano sul tuo username.",
        price=5.00,
    ),
    "ingame_5m": Product(
        id="ingame_5m",
        name="5M in-game",
        product_type=TYPE_INGAME,
        description="5 milioni di valuta in gioco, consegnati a mano sul tuo username.",
        price=15.00,
    ),
    "ingame_10m": Product(
        id="ingame_10m",
        name="10M in-game",
        product_type=TYPE_INGAME,
        description="10 milioni di valuta in gioco, consegnati a mano sul tuo username.",
        price=20.00,
    ),
}


def get_product(product_id: str) -> Product | None:
    return PRODUCTS.get(product_id)


def all_products() -> list[Product]:
    return list(PRODUCTS.values())


def products_by_type(product_type: str) -> list[Product]:
    return [p for p in PRODUCTS.values() if p.product_type == product_type]
