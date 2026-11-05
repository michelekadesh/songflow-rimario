# Rimario italiano

Il rimario dell'app SongFlow: un database SQLite con circa 150.000 parole italiane, con sillabe, posizione dell'accento, chiave di rima, chiave di assonanza e frequenza d'uso. Funziona senza internet.

## Come rigenerarlo

Serve solo Python 3. Scarica le fonti:

- Wiktionary (estrazione Kaikki): https://kaikki.org/dictionary/Italian/kaikki.org-dictionary-Italian.jsonl
- Morph-it!: https://docs.sslmit.unibo.it/lib/exe/fetch.php?media=resources:morph-it.tgz
- wordfreq: https://raw.githubusercontent.com/rspeer/wordfreq/master/wordfreq/data/large_it.msgpack.gz

Poi:

    python3 genera_rimario.py --kaikki italiano.jsonl --morphit morph-it_048.txt \
        --wordfreq large_it.msgpack.gz --offese offese.txt --uscita rimario.sqlite

Lo script controlla un elenco di parole verificate a mano e si ferma con un errore se non tornano.
Kaikki indica il suo file come in via di dismissione: se sparisce, serve il file grezzo di wiktextract,
che ha lo stesso formato.

## Licenza del database

CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Dati modificati: accenti, rime e assonanze
calcolati, parole offensive e rare escluse.

Fonti:

- **Wiktionary**, autori di Wiktionary, CC BY-SA 4.0, tramite kaikki.org (wiktextract, Tatu Ylonen).
- **Morph-it!**, Marco Baroni ed Eros Zanchetta, CC BY-SA 2.0, che consente versioni successive con le stesse condizioni.
- **wordfreq**, Robyn Speer, CC BY-SA 4.0. Comprende Wikipedia, OpenSubtitles, SUBTLEX (dati liberamente disponibili),
  NewsCrawl, GlobalVoices, Google Books Ngram Viewer (https://books.google.com/ngrams), OSCAR, Twitter e Reddit.

Lo script `genera_rimario.py` e l'elenco `offese.txt` sono offerti con la stessa licenza.

## Avvertenze

Offerto così com'è, senza garanzie. Parte degli accenti è calcolata con regole e può contenere errori: la colonna `origine` dice da dove viene ciascuno (1 Wiktionary, 2 tabelle di Wiktionary, 3 accento scritto, 4 flessione dal lemma, 5 regole). La licenza completa è in `LICENSE.txt`.
