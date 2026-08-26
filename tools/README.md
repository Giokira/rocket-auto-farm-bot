# tools/

Cartella del keygen per il **modello di licenza offline**
(`LICENSE_MODE=offline`), non usato nel setup attuale (che e' `online` — vedi
il README principale). Tenuta per compatibilita' se un giorno serve tornarci.
**Niente qui dentro va committato**, a parte questo file.

## Contratto atteso dal bot

Il bot esegue il comando in `KEYGEN_CMD` (default `java tools/Keygen.java {username}`)
sostituendo `{username}` con lo username Minecraft del compratore, e legge lo
standard output cercando una riga nella forma:

```
GKR1-<base64url-senza-padding>
```

Regole:

- il comando viene eseguito **senza shell**: niente pipe, niente redirezioni,
  niente `&&` dentro `KEYGEN_CMD`;
- exit code diverso da 0, timeout (`KEYGEN_TIMEOUT`, default 60s) o assenza
  della riga `GKR1-...` = consegna fallita, l'ordine resta aperto;
- lo username e' gia' validato (`^[A-Za-z0-9_]{3,16}$`) prima di arrivare qui.

## File presenti sulla macchina, mai nel repository

```
tools/Keygen.java              il tuo tool (puoi committarlo se non contiene segreti)
tools/giokiradd-private.key    CHIAVE PRIVATA: mai su VPS, mai su git, mai in backup pubblici
tools/keys/<username>.key      chiavi generate, una per compratore
```

Se `PRIVATE_KEY_PATH` non esiste, `keygen.py` fallisce di proposito con un
messaggio esplicito: e' la protezione contro il bot in esecuzione su un VPS,
dove la chiave privata non deve stare.
