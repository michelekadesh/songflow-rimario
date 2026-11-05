#!/usr/bin/env python3
"""Genera sinonimi.sqlite (sinonimi e contrari italiani) per SongFlow, dal Wikizionario.

Uso:
    python3 genera_sinonimi.py --wikizionario italiano.jsonl --wiktionary italiano-en.jsonl \
        --rimario rimario.sqlite --offese offese.txt --offese-sinonimi offese_sinonimi.txt --uscita sinonimi.sqlite

Serve solo Python 3. Il file --wikizionario e' l'estrazione kaikki.org dell'edizione italiana:
https://kaikki.org/itwiktionary/Italiano/kaikki.org-dictionary-Italiano.jsonl
--wiktionary e' lo stesso file usato per il rimario: serve a riconoscere le parole offensive.
Una parola vale se e' nel rimario o ha una pagina propria nel Wikizionario: cosi' cadono i refusi.
Lo script si ferma con un errore se l'elenco di parole controllate a mano non torna.
"""
import argparse
import json
import sqlite3
import sys
import unicodedata

SEPARATORE = "|"
ETICHETTE_OFFESA = {"offensive", "slur", "offensivo", "ingiurioso", "insulto"}
MASSIMO_PAROLE_ESPRESSIONE = 4
# Liste di un'altra parola rimaste sulla pagina: "sole" e' anche il plurale di "sola".
ERRORI_NOTI = {("sole", "pure"), ("sole", "senza compagnia"), ("sole", "per conto loro")}


def senza_accenti(testo):
    pezzi = unicodedata.normalize("NFD", testo)
    return "".join(c for c in pezzi if not unicodedata.combining(c)).lower()


def pulisci(testo):
    return testo.replace("’", "'").strip()


class Pagina:
    __slots__ = ("sinonimi", "contrari", "ha_lemma", "ha_forma", "lemmi", "sensi", "sensi_offensivi")

    def __init__(self):
        self.sinonimi = []
        self.contrari = []
        self.ha_lemma = False
        self.ha_forma = False
        self.lemmi = []
        self.sensi = 0
        self.sensi_offensivi = 0


def etichette_di(voce):
    return {t.strip().lower() for t in (voce.get("tags") or []) + (voce.get("raw_tags") or [])}


def aggiungi(lista, voci):
    for voce in voci or []:
        parola = pulisci(voce.get("word") or "")
        if not parola:
            continue
        if etichette_di(voce) & ETICHETTE_OFFESA:
            continue
        if parola not in lista:
            lista.append(parola)


def carica_pagine(percorso):
    pagine = {}
    with open(percorso, encoding="utf8") as file:
        for riga in file:
            voce = json.loads(riga)
            if voce.get("lang") != "Italiano":
                continue
            testa = pulisci(voce.get("word") or "").lower()
            if not testa:
                continue
            pagina = pagine.setdefault(testa, Pagina())
            aggiungi(pagina.sinonimi, voce.get("synonyms"))
            aggiungi(pagina.contrari, voce.get("antonyms"))
            sensi = voce.get("senses") or []
            forma = False
            for senso in sensi:
                pagina.sensi += 1
                if etichette_di(senso) & ETICHETTE_OFFESA:
                    pagina.sensi_offensivi += 1
                aggiungi(pagina.sinonimi, senso.get("synonyms"))
                aggiungi(pagina.contrari, senso.get("antonyms"))
                for origine in senso.get("form_of") or []:
                    lemma = pulisci(origine.get("word") or "").lower()
                    if lemma and lemma not in pagina.lemmi:
                        pagina.lemmi.append(lemma)
                if senso.get("form_of") or "form-of" in (senso.get("tags") or []):
                    forma = True
            if forma:
                pagina.ha_forma = True
            else:
                pagina.ha_lemma = True
    return pagine


def carica_rimario(percorso):
    database = sqlite3.connect("file:%s?mode=ro&immutable=1" % percorso, uri=True)
    note = set()
    comuni = set()
    for parola, normalizzata, comune in database.execute("SELECT parola, normalizzata, comune FROM parole"):
        note.add(parola)
        note.add(normalizzata)
        if comune:
            comuni.add(parola)
    database.close()
    return note, comuni


def offensive_wiktionary(percorso):
    """Le parole con tutti i sensi offensivi, come nel rimario."""
    sensi = {}
    with open(percorso, encoding="utf8") as file:
        for riga in file:
            if '"offensive"' not in riga and '"slur"' not in riga:
                # Una voce non segnata basta a dire che la parola non e' solo un'offesa.
                voce = json.loads(riga)
                sensi.setdefault((voce.get("word") or "").lower(), [0, 0])[0] += 1
                continue
            voce = json.loads(riga)
            conto = sensi.setdefault((voce.get("word") or "").lower(), [0, 0])
            for senso in voce.get("senses") or []:
                conto[0] += 1
                if etichette_di(senso) & ETICHETTE_OFFESA:
                    conto[1] += 1
    return {senza_accenti(p) for p, (tutti, offensivi) in sensi.items() if tutti and tutti == offensivi}


def carica_offese(percorso):
    """Le parole da togliere sempre e le coppie "testa > parola" da togliere solo insieme."""
    parole = set()
    coppie = set()
    with open(percorso, encoding="utf8") as file:
        for riga in file:
            riga = riga.split("#")[0].strip().lower()
            if ">" in riga:
                testa, parola = (senza_accenti(p.strip()) for p in riga.split(">", 1))
                coppie.add((testa, parola))
            elif riga:
                parole.add(senza_accenti(riga))
    return parole, coppie


def valida(parola, testa, nota, offese, statistiche):
    """Vero se il suggerimento e' una parola o un'espressione vera e non offensiva."""
    basso = parola.lower()
    if basso == testa or senza_accenti(basso) == senza_accenti(testa):
        return False
    if not parola[0].isalpha() or any(c.isdigit() or c in "()[]{}/:;=#\"" for c in parola) or len(parola) < 2:
        statistiche["simboli"] += 1
        return False
    pezzi = [p for p in basso.replace("'", " ").split(" ") if p]
    if not pezzi or len(pezzi) > MASSIMO_PAROLE_ESPRESSIONE:
        statistiche["troppo_lunghe"] += 1
        return False
    if any(senza_accenti(p) in offese for p in pezzi):
        statistiche["offese"] += 1
        return False
    # Senza pagina e fuori dal rimario: quasi sempre un refuso ("fedddezza").
    if not nota(basso) if len(pezzi) == 1 else not all(nota(p) or len(p) <= 2 for p in pezzi):
        statistiche["sconosciute"] += 1
        return False
    return True


def ordina(lista):
    """L'ordine del Wikizionario mette prima il significato principale; le espressioni vanno in fondo."""
    return [p for p in lista if " " not in p] + [p for p in lista if " " in p]


def costruisci(pagine, note, comuni, offese, coppie, statistiche):
    def nota(parola):
        return parola in note or parola in pagine

    voci = {}
    for testa, pagina in pagine.items():
        if senza_accenti(testa) in offese:
            continue
        def accettata(parola):
            if (testa, parola.lower()) in ERRORI_NOTI:
                return False
            if (senza_accenti(testa), senza_accenti(parola)) in coppie:
                statistiche["offese"] += 1
                return False
            return valida(parola, testa, nota, offese, statistiche)

        sinonimi = [p for p in pagina.sinonimi if accettata(p)]
        contrari = [p for p in pagina.contrari if accettata(p)]
        # Pagina con un lemma e una forma flessa ("sole": l'astro e il plurale di "sola").
        # Le liste sono della pagina intera: le forme flesse appartengono all'altra parola.
        if pagina.ha_lemma and pagina.ha_forma:
            def del_lemma(parola):
                altra = pagine.get(parola.lower())
                return " " in parola or altra is None or altra.ha_lemma
            prima = len(sinonimi) + len(contrari)
            sinonimi = [p for p in sinonimi if del_lemma(p)]
            contrari = [p for p in contrari if del_lemma(p)]
            statistiche["forme_altrui"] += prima - len(sinonimi) - len(contrari)
        # Una stessa parola non puo' essere sinonimo e contrario: e' un errore di estrazione.
        insieme = {p.lower() for p in sinonimi}
        tolti = [p for p in contrari if p.lower() in insieme]
        statistiche["contrari_doppi"] += len(tolti)
        contrari = [p for p in contrari if p.lower() not in insieme]
        if sinonimi or contrari:
            voci[testa] = (ordina(sinonimi), ordina(contrari))

    # Le forme senza lista propria ("amori") mandano alla parola base. Solo le parole comuni.
    forme = {}
    for testa, pagina in pagine.items():
        if testa in voci or testa not in comuni or senza_accenti(testa) in offese:
            continue
        coda = list(pagina.lemmi)
        for lemma in coda:
            if lemma in voci:
                forme[testa] = lemma
                break
            altra = pagine.get(lemma)
            if altra and len(coda) < 8:
                coda.extend(l for l in altra.lemmi if l not in coda)
    return voci, forme


# Controllati a mano: se non tornano, la pulizia ha rotto qualcosa.
SINONIMI_ATTESI = {
    "felice": ["contento", "lieto"], "amore": ["affetto", "passione"], "triste": ["malinconico"],
    "notte": ["oscurità"], "luce": ["chiarore"], "felici": ["contenti"], "paura": ["timore"],
    "sole": ["astro"], "bello": ["attraente"], "piangere": ["singhiozzare"], "mare": ["oceano"],
}
CONTRARI_ATTESI = {
    "felice": ["triste", "infelice"], "amore": ["odio"], "notte": ["giorno"], "vita": ["morte"],
    "luce": ["buio"], "forte": ["debole"], "freddo": ["caldo"], "grande": ["piccolo"], "sole": ["buio"],
}
NON_ATTESI = {
    "sole": ["solitarie", "isolate", "pure"], "cuore": ["fedddezza"],
    "omosessuale": ["culattone", "checca", "finocchio", "invertito"],
    "prostituta": ["vacca", "mignotta", "sgualdrina"], "disabile": ["handicappato", "impedito"],
}
FORME_ATTESE = {"amori": "amore", "piango": "piangere"}


def controlla(voci, forme):
    errori = []
    for testa, attesi in SINONIMI_ATTESI.items():
        for parola in attesi:
            if parola not in voci.get(testa, ([], []))[0]:
                errori.append("%s: manca il sinonimo %s" % (testa, parola))
    for testa, attesi in CONTRARI_ATTESI.items():
        for parola in attesi:
            if parola not in voci.get(testa, ([], []))[1]:
                errori.append("%s: manca il contrario %s" % (testa, parola))
    for testa, vietate in NON_ATTESI.items():
        s, c = voci.get(testa, ([], []))
        for parola in vietate:
            if parola in s or parola in c:
                errori.append("%s: non deve contenere %s" % (testa, parola))
    for forma, lemma in FORME_ATTESE.items():
        if forme.get(forma) != lemma:
            errori.append("%s: deve mandare a %s, invece %s" % (forma, lemma, forme.get(forma)))
    return errori


def scrivi(uscita, voci, forme):
    database = sqlite3.connect(uscita)
    database.executescript(
        """
        DROP TABLE IF EXISTS voci; DROP TABLE IF EXISTS forme; DROP TABLE IF EXISTS info;
        CREATE TABLE voci (parola TEXT PRIMARY KEY, normalizzata TEXT NOT NULL,
                           sinonimi TEXT NOT NULL, contrari TEXT NOT NULL) WITHOUT ROWID;
        CREATE INDEX voci_normalizzata ON voci (normalizzata);
        CREATE TABLE forme (forma TEXT PRIMARY KEY, normalizzata TEXT NOT NULL, lemma TEXT NOT NULL) WITHOUT ROWID;
        CREATE INDEX forme_normalizzata ON forme (normalizzata);
        CREATE TABLE info (chiave TEXT PRIMARY KEY, valore TEXT) WITHOUT ROWID;
        """
    )
    database.executemany(
        "INSERT INTO voci VALUES (?,?,?,?)",
        (
            (t, senza_accenti(t), SEPARATORE.join(s), SEPARATORE.join(c))
            for t, (s, c) in sorted(voci.items())
        ),
    )
    database.executemany(
        "INSERT INTO forme VALUES (?,?,?)",
        ((f, senza_accenti(f), l) for f, l in sorted(forme.items())),
    )
    database.executemany(
        "INSERT INTO info VALUES (?,?)",
        [
            ("fonte", "Wikizionario, autori del Wikizionario, tramite kaikki.org (wiktextract, Tatu Ylonen)"),
            ("licenza", "CC BY-SA 4.0"),
            ("modifiche", "pulizia di refusi ed errori di estrazione, offese tolte, espressioni spostate in fondo"),
        ],
    )
    database.commit()
    database.execute("VACUUM")
    database.close()


def main():
    argomenti = argparse.ArgumentParser()
    argomenti.add_argument("--wikizionario", required=True)
    argomenti.add_argument("--wiktionary", required=True)
    argomenti.add_argument("--rimario", required=True)
    argomenti.add_argument("--offese", required=True)
    argomenti.add_argument("--offese-sinonimi", required=True)
    argomenti.add_argument("--uscita", required=True)
    a = argomenti.parse_args()

    statistiche = dict.fromkeys(
        ("simboli", "troppo_lunghe", "offese", "sconosciute", "forme_altrui", "contrari_doppi"), 0
    )
    pagine = carica_pagine(a.wikizionario)
    note, comuni = carica_rimario(a.rimario)
    # Una parola con tutti i sensi offensivi, in una qualunque delle due fonti, non entra.
    offese = carica_offese(a.offese)[0] | offensive_wiktionary(a.wiktionary)
    aggiunte, coppie = carica_offese(a.offese_sinonimi)
    offese |= aggiunte
    offese |= {senza_accenti(t) for t, p in pagine.items() if p.sensi and p.sensi == p.sensi_offensivi}
    voci, forme = costruisci(pagine, note, comuni, offese, coppie, statistiche)

    errori = controlla(voci, forme)
    if errori:
        print("\n".join(errori), file=sys.stderr)
        sys.exit("Controllo non superato: %d errori. Database non scritto." % len(errori))

    scrivi(a.uscita, voci, forme)
    print("pagine", len(pagine), "voci", len(voci), "forme", len(forme))
    print("con sinonimi", sum(1 for s, _ in voci.values() if s), "con contrari", sum(1 for _, c in voci.values() if c))
    print("coppie", sum(len(s) for s, _ in voci.values()), sum(len(c) for _, c in voci.values()))
    print("tolte", statistiche)


if __name__ == "__main__":
    main()
