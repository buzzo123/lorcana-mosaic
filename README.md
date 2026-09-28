# lorcana-mosaic

Fotomosaici costruiti esclusivamente con immagini di carte Disney Lorcana, prese dalle
API [Lorcast](https://lorcast.com/docs/api).

Stessa idea di `sausheong/mosaic` e `White-On/Mosaic_Image`, con tre differenze
sostanziali dettate dal dominio:

1. **I tile non sono quadrati.** Le carte Lorcana sono 1468×2048 (≈ 5:7). La griglia
   usa tile verticali e le righe vengono calcolate per mantenere le proporzioni
   dell'immagine sorgente. Le *Location* (layout `landscape`) vengono escluse
   automaticamente, altrimenti sfonderebbero la griglia.
2. **La libreria è piccola e cromaticamente concentrata.** Poche migliaia di carte,
   con sei famiglie di inchiostro che dominano il colore medio. Senza contromisure il
   matching "prendi sempre la carta più vicina" collassa su una manciata di carte.
3. **La varietà è un parametro di primo livello**, non un effetto collaterale. Vedi sotto.

## Installazione

Con [uv](https://docs.astral.sh/uv/) (consigliato):

```bash
uv sync                       # crea .venv e installa tutto
source .venv/bin/activate     # oppure prefissa i comandi con `uv run`
```

Oppure con pip:

```bash
pip install -r requirements.txt
```

Le immagini Lorcast sono in **AVIF**: serve Pillow ≥ 11.3 (supporto AVIF incluso)
oppure `pip install pillow-avif-plugin`. Il programma controlla e lo dice subito se manca.

## Uso

```bash
# 1. scarica catalogo + immagini nella cache (~/.cache/lorcana-mosaic)
python -m lorcana_mosaic fetch --size normal

# 2. costruisci il mosaico
python -m lorcana_mosaic build foto.jpg -o mosaico.jpg --cols 60 --tile-width 120
```

Il primo comando rispetta il rate limit chiesto da Lorcast (≈ 100 ms tra le chiamate a
`api.lorcast.com`) e scarica le immagini in parallelo dalla CDN `cards.lorcast.io`, che
invece non ha limiti. Tutto viene messo in cache: catalogo, immagini e feature colore.
Un refresh settimanale è più che sufficiente, come suggerisce la documentazione.

Varianti utili:

```bash
# solo set 1 e 2, solo Ambra e Rubino
python -m lorcana_mosaic fetch --set 1 --set 2 --ink Amber --ink Ruby

# usa la sintassi di ricerca Lorcast
python -m lorcana_mosaic fetch --query "set:1 rarity:enchanted"

# cosa c'è in cache
python -m lorcana_mosaic info
```

## Come funziona il matching

Ogni carta e ogni cella della griglia vengono ridotte a una firma di **3×3 colori medi
in CIELAB** (27 numeri), non a un singolo colore medio: così il confronto tiene conto
anche della struttura interna del tile, e una carta chiara in alto e scura in basso non
viene scambiata per una grigia uniforme.

Le distanze si calcolano con una matmul a blocchi (`‖a‖² + ‖b‖² − 2a·b`) e per ogni cella
si tiene solo una shortlist dei migliori candidati, quindi la memoria non esplode sulle
griglie grandi.

`--grid 4` o `5` aumenta la precisione strutturale; `--grid 1` torna al classico
colore medio.

## Varietà (il punto richiesto)

Quattro meccanismi indipendenti, tutti disattivabili:

| Opzione | Cosa fa |
|---|---|
| `--max-reuse N` | Tetto massimo di utilizzi per singola carta. Default *auto* (≈ 2× il minimo necessario). `0` = illimitato. |
| `--min-distance N` | Vieta la ricomparsa della stessa carta entro N celle (distanza di Chebyshev). Con `--repel-by name` (default) la regola vale per *personaggio*, quindi non si ammassano nemmeno le varianti della stessa Elsa. |
| `--candidates K` + `--tolerance dE` | Invece di prendere sempre il primo match, sceglie tra i K migliori **che costano al massimo `dE` per cella** rispetto al migliore. La varietà entra solo dove è quasi gratis. |
| `--temperature T` | Campionamento softmax tra i candidati ammessi (in dE per cella). `0` = prendi sempre il migliore ammesso. |

Due dettagli implementativi che contano più di quanto sembri:

* **Le celle difficili scelgono per prime** (`--order hard-first`). Altrimenti cieli e muri
  consumano il budget di riutilizzo delle poche carte scure che servono a un occhio o a un'ombra.
* **Quando i vincoli non sono soddisfacibili** per una cella, il fallback ricerca su
  tutta la libreria rispettando comunque il tetto di riutilizzo, invece di ripiegare
  sempre sulla stessa carta. Il report finale dice quante celle hanno richiesto un
  rilassamento: se il numero è alto, la libreria è troppo piccola per i vincoli imposti.

In più, `--dedupe-art 2` elimina le ristampe con arte identica (confronto dhash **e**
firma colore, per evitare falsi positivi sulle illustrazioni molto uniformi).

Il trade-off è reale ed è misurato a ogni run. Dati da un test su una libreria
sintetica di 500 carte, griglia 30×29 = 870 celle:

| Configurazione | Carte distinte | Max usi | Errore medio |
|---|---|---|---|
| `--tolerance 0 --temperature 0 --min-distance 0 --max-reuse 0` (classico) | 26 | 473 | 32.3 |
| default (`--max-reuse auto`) | 247 | 4 | 43.7 |
| `--max-reuse 8` | 178 | 8 | 40.6 |
| `--max-reuse 2` | 440 | 2 | 49.8 |
| `--unique` | tutte | 1 | massimo |

Tradotto: più varietà = più errore cromatico. La via di mezzo migliore non è un
compromesso sui vincoli ma la **correzione colore**, qui sotto.

## Fedeltà

Con una libreria di poche migliaia di carte dai colori saturi, il mosaico "puro"
difficilmente somiglia molto alla foto. Due correzioni, entrambe standard nei
fotomosaici, entrambe graduabili:

* `--blend 0.25` — sovrappone a ogni carta una tinta piatta pari al colore medio della
  cella. Migliora moltissimo la leggibilità a distanza, la carta resta riconoscibile.
* `--overlay 0.2` — sovrappone il ritaglio reale della foto. Più aggressivo, recupera
  anche il dettaglio fine.

E soprattutto `--crop`:

* `--crop full` (default) — carta intera: il risultato si legge come un muro di carte.
* `--crop art` — solo l'illustrazione, senza cornice e box del testo. Molto più vicino
  alla foto, perché il colore non è più dominato dalla cornice dell'inchiostro.
* `--crop-box L T R B` — ritaglio custom in frazioni, se vuoi tarare la finestra.

Buon punto di partenza per un risultato "riconoscibile a distanza ma variegato":

```bash
python -m lorcana_mosaic build foto.jpg -o mosaico.jpg \
  --cols 70 --tile-width 140 --crop art --blend 0.22 \
  --max-reuse 6 --min-distance 4 --tolerance 6 --dedupe-art 2 \
  --manifest mosaico.json
```

`--manifest` scrive un JSON con quale carta sta in quale cella: comodo se poi vuoi
costruirlo davvero, o generare una lista della spesa.

Per lo stile "collezione" con le carte staccate: `--gap 3 --bg "#0d0d0d"`.

## Test offline

Non serve la rete per provare la pipeline:

```bash
uv run tools/make_fake_cache.py --cache /tmp/fakecache --count 500
uv run python -m lorcana_mosaic --cache /tmp/fakecache build foto.jpg -o out.jpg --cols 30
```

## Note

* Le immagini restano di Ravensburger/Disney. Lorcast le distribuisce sotto la
  [Community Code Policy](https://cdn.ravensburger.com/lorcana/community-code-en) di
  Ravensburger, che vieta espressamente di far pagare l'accesso a questi contenuti:
  progetto personale/non commerciale, e non mettere in vendita i mosaici.
* L'API Lorcast è in beta `v0` e non paginata: `/cards/search` potrebbe diventarlo in
  futuro. Il client usa `/sets` + `/sets/:code/cards`, che oggi restituisce tutto.
* `--size small` (146×204) basta per griglie fitte ed è molto più veloce da scaricare e
  analizzare; `normal` (488×681) è il compromesso giusto per la stampa.
