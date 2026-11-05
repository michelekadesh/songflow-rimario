#!/usr/bin/env python3
"""Genera il database del rimario italiano (rimario.sqlite), usato dall'app senza internet.

Uso:
    python3 genera_rimario.py --kaikki italiano.jsonl --morphit morph-it_048.txt \\
        --wordfreq large_it.msgpack.gz --offese offese.txt --uscita rimario.sqlite

Le fonti si scaricano da:
    https://kaikki.org/dictionary/Italian/kaikki.org-dictionary-Italian.jsonl
    https://docs.sslmit.unibo.it/lib/exe/fetch.php?media=resources:morph-it.tgz
    https://raw.githubusercontent.com/rspeer/wordfreq/master/wordfreq/data/large_it.msgpack.gz

Da dove viene l'accento di una parola, dal più sicuro al meno sicuro:
    1 sillabe o pronuncia di Wiktionary    2 tabelle di flessione di Wiktionary
    3 accento scritto sull'ultima vocale   4 flessione dal lemma (nomi e aggettivi)
    5 regole sulle desinenze verbali (Morph-it!)
Le parole in cui l'accento resta ignoto non entrano nel database.

Solo libreria standard di Python 3.
"""
import argparse
import collections
import gzip
import json
import re
import sqlite3
import struct
import unicodedata

VOCALI = "aeiouy"
SEGNI_ACCENTO = {"̀", "́"}
SEPARATORI = re.compile(r"[‧·.\-|]")
SOLO_LETTERE = re.compile(r"^[a-zàáèéìíòóùúïü']+$")
ETICHETTE_RARE = {"archaic", "obsolete", "rare", "dated", "dialectal", "literary", "regional"}
# Solo le offese vere: "spregiativo" su Wiktionary include troppe parole normali (scartoffia, politicante).
ETICHETTE_OFFESA = {"offensive", "slur"}
# Sotto questa frequenza (scala Zipf x 100) una parola non viene proposta come rima: resta solo cercabile.
SOGLIA_COMUNE = 200
# Un accento alternativo si tiene solo se il suo lemma non è molto più raro di quello principale (idèa / ìdea).
SCARTO_MASSIMO_VARIANTE = 150
# Articoli, preposizioni e congiunzioni: si possono cercare ma non sono buoni suggerimenti.
POS_GRAMMATICALI = {"article", "prep", "conj", "det", "particle", "contraction"}
# Poche parole grammaticali comuni che nessuna fonte accenta.
ACCENTI_FISSI = {"cui": 1, "sui": 1, "coi": 1, "dei": 1, "nei": 1, "lui": 1, "lei": 1, "noi": 1, "voi": 1, "poi": 1, "tuo": 1, "tue": 1}
PAROLE_NON_ITALIANE = {"the", "of", "and", "you", "to", "on", "new", "for", "my", "be", "at"}
# Si possono cercare, ma come rima suggerita non servono: articoli, preposizioni e particelle.
PAROLE_SOLO_CERCABILI = PAROLE_NON_ITALIANE | {
    "a", "ad", "al", "da", "dal", "di", "del", "e", "ed", "o", "od", "i", "il", "lo", "la", "le", "li", "gli",
    "ma", "se", "che", "su", "sul", "in", "per", "con", "col", "tra", "fra", "ne", "si", "ci", "vi", "mi", "ti",
    "un", "uno", "una", "non",
}

# ---- Lettere e accenti ------------------------------------------------------

def senza_accenti(testo):
    return "".join(c for c in unicodedata.normalize("NFD", testo) if unicodedata.category(c) != "Mn")


def posizione_accento(accentata):
    """L'indice (nella parola senza accenti) dell'ultima vocale con il segno grave o acuto."""
    trovata = None
    indice = -1
    for carattere in unicodedata.normalize("NFD", accentata):
        if unicodedata.category(carattere) == "Mn":
            if carattere in SEGNI_ACCENTO and indice >= 0:
                trovata = indice
        else:
            indice += 1
    return trovata


def nuclei(parola, tonica):
    """Le sillabe come nuclei vocalici: restituisce le posizioni (inizio, fine) di ogni nucleo."""
    risultato = []
    i, n = 0, len(parola)
    while i < n:
        if parola[i] not in VOCALI:
            i += 1
            continue
        j = i + 1
        while j < n and parola[j] in VOCALI:
            j += 1
        risultato.extend(dividi_gruppo(parola, i, j, tonica))
        i = j
    return risultato


def dividi_gruppo(parola, inizio, fine, tonica):
    """Divide un gruppo di vocali vicine in sillabe, con la divisione scolastica:
    forti separate (po-e-ta), i e u deboli unite (die-ci, au-to), i tra vocali in sillaba nuova
    (pa-io, a-iu-to), i o u con l'accento separate (mi-o, co-stru-i-re) tranne dopo c, g, q."""
    gruppi = []
    corrente = inizio
    k = inizio + 1
    while k < fine:
        a, b = parola[k - 1], parola[k]
        b_forte = b in "aeo"
        a_forte = a in "aeo"
        separa = a_forte and b_forte
        # Tonica debole dopo una forte (pa-ù-ra) o dopo una u (co-stru-ì-re, ma gui-da e qui).
        if k == tonica and not b_forte:
            dopo_u = a == "u" and not u_muta(parola, k - 1) and not (k >= 2 and parola[k - 2] == "g")
            if a_forte or dopo_u:
                separa = True
        if k - 1 == tonica and not a_forte and not i_muta(parola, k - 1):
            separa = True
        # La i tra due vocali apre una sillaba: pa-io, mi-glia-ia.
        if b == "i" and a_forte and k + 1 < fine and k != tonica:
            separa = True
        if separa:
            gruppi.append((corrente, k))
            corrente = k
        k += 1
    gruppi.append((corrente, fine))
    return gruppi


def i_muta(parola, k):
    """La i di "cia", "gio", "scia", "glia" non si pronuncia: non conta per l'assonanza."""
    if parola[k] != "i" or k == 0 or k + 1 >= len(parola) or parola[k + 1] not in "aeou":
        return False
    return parola[k - 1] in "cg" or parola[max(k - 2, 0):k] == "sc" or parola[max(k - 2, 0):k] == "gl"


def u_muta(parola, k):
    return parola[k] == "u" and k > 0 and parola[k - 1] == "q"


def conta_sillabe(parola, tonica):
    return max(len(nuclei(parola, tonica)), 1)


def tipo_parola(parola, tonica):
    """0 tronca, 1 piana, 2 sdrucciola, 3 bisdrucciola."""
    lista = nuclei(parola, tonica)
    for indice, (a, b) in enumerate(lista):
        if a <= tonica < b:
            return min(len(lista) - 1 - indice, 3)
    return 1


def chiave_rima(parola, tonica):
    return parola[tonica:]


def chiave_assonanza(parola, tonica):
    vocali = [
        c for k, c in enumerate(parola[tonica:], start=tonica)
        if c in VOCALI and (k == tonica or not (i_muta(parola, k) or u_muta(parola, k)))
    ]
    return "".join(vocali)


def tonica_monosillabo(parola):
    """Una sola sillaba: l'accento cade sulla vocale forte, altrimenti sull'ultima."""
    posizioni = [k for k, c in enumerate(parola) if c in VOCALI and not u_muta(parola, k) and not i_muta(parola, k)]
    if not posizioni:
        return None
    forti = [k for k in posizioni if parola[k] in "aeo"]
    return (forti or posizioni)[-1 if not forti else 0]


def tonica_da_ipa(parola, ipa):
    """Dalla pronuncia con punti di sillaba (/ˈpar.la.no/): conta le sillabe dalla fine."""
    testo = ipa.strip("/[]").replace("ˌ", "")
    if "ˈ" not in testo:
        return None
    dopo = testo.split("ˈ")[-1]
    sillabe_dopo = dopo.count(".")
    lista = nuclei(parola, None)
    if sillabe_dopo >= len(lista):
        return None
    a, b = lista[len(lista) - 1 - sillabe_dopo]
    forti = [k for k in range(a, b) if parola[k] in "aeo"]
    if forti:
        return forti[0]
    utili = [k for k in range(a, b) if not u_muta(parola, k) and not i_muta(parola, k)]
    return (utili or [a])[-1]


# ---- wordfreq: un piccolo lettore del formato msgpack ------------------------

def leggi_msgpack(dati, p=0):
    b = dati[p]
    p += 1
    if b <= 0x7f:
        return b, p
    if 0x90 <= b <= 0x9f:
        n = b & 0x0f
    elif b == 0xdc:
        n = struct.unpack(">H", dati[p:p + 2])[0]; p += 2
    elif b == 0xdd:
        n = struct.unpack(">I", dati[p:p + 4])[0]; p += 4
    elif 0x80 <= b <= 0x8f or b == 0xde:
        if b == 0xde:
            n = struct.unpack(">H", dati[p:p + 2])[0]; p += 2
        else:
            n = b & 0x0f
        mappa = {}
        for _ in range(n):
            k, p = leggi_msgpack(dati, p)
            v, p = leggi_msgpack(dati, p)
            mappa[k] = v
        return mappa, p
    elif 0xa0 <= b <= 0xbf or b in (0xd9, 0xda, 0xdb):
        if b == 0xd9:
            n = dati[p]; p += 1
        elif b == 0xda:
            n = struct.unpack(">H", dati[p:p + 2])[0]; p += 2
        elif b == 0xdb:
            n = struct.unpack(">I", dati[p:p + 4])[0]; p += 4
        else:
            n = b & 0x1f
        return dati[p:p + n].decode("utf8"), p + n
    elif b == 0xcc:
        return dati[p], p + 1
    else:
        raise ValueError("msgpack: tipo %x" % b)
    lista = []
    for _ in range(n):
        v, p = leggi_msgpack(dati, p)
        lista.append(v)
    return lista, p


def carica_frequenze(percorso):
    """La lista "cBpack": la posizione i vuol dire frequenza 10^(-i/100). Restituisce Zipf x 100."""
    dati = gzip.decompress(open(percorso, "rb").read())
    pacchetto, _ = leggi_msgpack(dati)
    frequenze = {}
    for i, parole in enumerate(pacchetto[1:]):
        zipf = 900 - i
        for parola in parole:
            frequenze.setdefault(parola, zipf)
    return frequenze


# ---- Wiktionary --------------------------------------------------------------

class Voce:
    __slots__ = ("toniche", "origine", "sillabe", "pos", "etichette_tutte", "offesa_tutte", "lemmi", "ha_sensi", "pesi")

    def __init__(self):
        self.toniche = {}       # posizione -> origine (la più sicura)
        self.origine = 9
        self.sillabe = {}       # posizione -> numero di sillabe di Wiktionary
        self.pos = set()
        self.etichette_tutte = True   # tutti i sensi sono rari o antichi
        self.offesa_tutte = True      # tutti i sensi sono offensivi
        self.lemmi = set()
        self.ha_sensi = False
        self.pesi = {}          # posizione -> lemmi che portano a quell'accento

    def aggiungi(self, tonica, origine, sillabe=None, lemmi=None):
        if tonica is None:
            return
        self.pesi.setdefault(tonica, set()).update(lemmi or {""})
        if origine < self.toniche.get(tonica, 9):
            self.toniche[tonica] = origine
        if sillabe:
            self.sillabe[tonica] = sillabe


def carica_wiktionary(percorso, voci, statistiche):
    per_lemma_nomi = []
    for riga in open(percorso, encoding="utf8"):
        d = json.loads(riga)
        parola_originale = d.get("word") or ""
        pos = d.get("pos")
        # Niente nomi propri, sigle, prefissi e locuzioni.
        if pos in {"name", "prefix", "suffix", "phrase", "prep_phrase", "proverb", "character", "symbol", "abbrev"}:
            continue
        if parola_originale != parola_originale.lower() or not SOLO_LETTERE.match(parola_originale):
            continue
        parola = parola_originale.strip("'")
        if not parola:
            continue
        chiave = senza_accenti(parola)
        voce = voci.setdefault(parola, Voce())
        voce.pos.add(pos)

        lemmi_voce = set()
        for senso in d.get("senses") or []:
            etichette = set(senso.get("tags") or [])
            voce.ha_sensi = True
            if not etichette & ETICHETTE_RARE:
                voce.etichette_tutte = False
            if not etichette & ETICHETTE_OFFESA:
                voce.offesa_tutte = False
            forme_di = senso.get("form_of") or []
            for forma_di in forme_di:
                voce.lemmi.add(forma_di.get("word") or "")
                lemmi_voce.add(forma_di.get("word") or "")
            if not forme_di:
                lemmi_voce.add(parola)

        # 1a: le sillabe con l'accento scritto.
        trovata = False
        for testo in d.get("hyphenation") or []:
            parti = SEPARATORI.split(testo)
            unita = "".join(parti)
            if senza_accenti(unita).lower() != chiave:
                continue
            tonica = posizione_accento(unita.lower())
            if tonica is None and len(parti) == 1:
                tonica = tonica_monosillabo(chiave)
            if tonica is not None:
                voce.aggiungi(tonica, 1, len(parti), lemmi_voce)
                trovata = True

        # 1b: la pronuncia, solo se le sillabe non dicono niente.
        pronunce = [s["ipa"] for s in d.get("sounds") or [] if "ipa" in s]
        for ipa in pronunce:
            t = tonica_da_ipa(chiave, ipa)
            if t is not None:
                if trovata:
                    statistiche["ipa_confrontate"] += 1
                    if t not in voce.toniche:
                        statistiche["ipa_diverse"] += 1
                else:
                    voce.aggiungi(t, 1, None, lemmi_voce)

        # 2: le tabelle di flessione e le forme nominate nella voce.
        nominali = pos in {"noun", "adj"}
        for forma in d.get("forms") or []:
            testo = forma.get("form") or ""
            etichette = set(forma.get("tags") or [])
            if not testo or " " in testo or etichette & {"inflection-template", "table-tags", "class", "auxiliary"}:
                continue
            testo = testo.lower()
            if not SOLO_LETTERE.match(testo):
                continue
            tonica = posizione_accento(testo)
            pulita = senza_accenti(testo)
            if tonica is not None:
                # "sonò" è una parola diversa da "sono": l'accento finale si scrive sempre.
                nome = testo if tonica == len(pulita) - 1 else pulita
                altra = voci.setdefault(nome, Voce())
                altra.aggiungi(tonica, 2, None, {chiave})
            elif nominali and "plural" in etichette:
                altra = voci.setdefault(pulita, Voce())
                altra.lemmi.add(chiave)
    return per_lemma_nomi


# ---- Morph-it! ---------------------------------------------------------------

# Forme verbali in cui la posizione dell'accento dipende solo dalla desinenza.
REGOLE_VERBI = [
    ("ind+impf+1+p", "vamo", 3), ("ind+impf+2+p", "vate", 3), ("ind+impf+1+s", "vo", 1 + 1), ("ind+impf+2+s", "vi", 2),
    ("ind+impf+3+s", "va", 2), ("ind+impf+3+p", "vano", 3),
    ("ind+fut+1+p", "remo", 3), ("ind+fut+2+p", "rete", 3), ("ind+fut+3+p", "ranno", 4), ("ind+fut+2+s", "rai", 2),
    ("cond+pres+1+s", "rei", 2), ("cond+pres+2+s", "resti", 4), ("cond+pres+3+s", "rebbe", 4), ("cond+pres+1+p", "remmo", 4),
    ("cond+pres+2+p", "reste", 4), ("cond+pres+3+p", "rebbero", 6),
    ("sub+impf+1+s", "ssi", 2), ("sub+impf+3+s", "sse", 2), ("sub+impf+1+p", "ssimo", 2), ("sub+impf+2+p", "ste", 1),
    ("sub+impf+3+p", "ssero", 2), ("ger+pres", "ndo", 1), ("part+past", "to", 1), ("ind+past+2+p", "ste", 1),
]


def tonica_da_desinenza(parola, etichetta):
    """Dove la desinenza decide da sola la posizione dell'accento."""
    if "part+past" in etichetta or "ger" in etichetta:
        lista = nuclei(parola, None)
        if len(lista) >= 2:
            return prima_forte(parola, lista[-2])
        return None
    for segno, finale, _ in REGOLE_VERBI:
        if segno in etichetta and parola.endswith(finale):
            radice = parola[: -len(finale)]
            if "fut" in segno or "cond" in segno:
                # parleremo: la tonica è la e di -remo; parlerebbe: la e di -rebbe.
                k = len(radice) + 1
                return k if k < len(parola) and parola[k] in VOCALI else None
            for k in range(len(radice) - 1, -1, -1):
                if radice[k] in VOCALI:
                    return k
    return None


def prima_forte(parola, nucleo):
    a, b = nucleo
    forti = [k for k in range(a, b) if parola[k] in "aeo"]
    if forti:
        return forti[0]
    utili = [k for k in range(a, b) if not u_muta(parola, k) and not i_muta(parola, k)]
    return (utili or [a])[-1]


def carica_morphit(percorso, voci, statistiche):
    per_lemma = []
    conosciute = set()
    for riga in open(percorso, encoding="latin-1"):
        pezzi = riga.rstrip("\n").split("\t")
        if len(pezzi) != 3:
            continue
        forma, lemma, etichetta = pezzi
        if forma != forma.lower() or lemma != lemma.lower() or not SOLO_LETTERE.match(forma):
            continue
        if etichetta.startswith(("NPR", "ABL", "ASP", "AUX", "CAU", "MOD")) and not etichetta.startswith(("AUX:", "MOD:", "ASP:", "CAU:")):
            continue
        forma = forma.strip("'")
        if not forma:
            continue
        conosciute.add(forma)
        per_lemma.append((forma, lemma, etichetta))
        if etichetta.startswith(("ART", "PRE", "CON")):
            voci.setdefault(forma, Voce()).pos.add("prep")
    return per_lemma, conosciute


def applica_regole(voci, per_lemma, frequenze, statistiche):
    """Solo dove Wiktionary non basta: flessione di nomi e aggettivi, poi desinenze verbali."""
    def tonica_flessione(forma, lemma):
        voce = voci.get(lemma)
        if not voce or not voce.toniche:
            return None
        chiave = senza_accenti(forma)
        pulito = senza_accenti(lemma)
        comune = 0
        while comune < min(len(chiave), len(pulito)) and chiave[comune] == pulito[comune]:
            comune += 1
        migliori = [t for t in voce.toniche if t < comune]
        if len(voce.toniche) == 1 and migliori and comune >= len(pulito) - 2:
            return migliori[0]
        return None

    def tonica_certa(parola):
        """Accento scritto in fondo, oppure una sola sillaba: non ci sono dubbi."""
        t = posizione_accento(parola)
        chiave = senza_accenti(parola)
        if t == len(chiave) - 1:
            return t
        if chiave in ACCENTI_FISSI:
            return ACCENTI_FISSI[chiave]
        # Una sola vocale: nessun dubbio. Con più vocali (mia, due) serve un'altra fonte.
        posizioni = [k for k, c in enumerate(chiave) if c in VOCALI]
        return posizioni[0] if len(posizioni) == 1 else None

    for parola, voce in list(voci.items()):
        t = tonica_certa(parola)
        if t is not None and not voce.toniche:
            voce.aggiungi(t, 3)

    for parola, voce in list(voci.items()):
        if voce.toniche:
            continue
        for lemma in voce.lemmi:
            if "verb" not in voce.pos:
                t = tonica_flessione(parola, lemma)
                if t is not None:
                    voce.aggiungi(t, 4, None, {lemma})

    for forma, lemma, etichetta in per_lemma:
        voce = voci.setdefault(forma, Voce())
        if voce.toniche:
            continue
        if etichetta.startswith(("NOUN", "ADJ")):
            if "sup" in etichetta and "issim" in forma:
                k = forma.rfind("issim")
                voce.aggiungi(k, 4)
                continue
            t = tonica_flessione(forma, lemma)
            if t is not None:
                voce.aggiungi(t, 4, None, {lemma})
        elif etichetta.startswith(("VER", "AUX", "MOD", "CAU", "ASP")):
            # Infinito con pronome: vederlo, parlarne. L'accento resta quello dell'infinito.
            radice = lemma[:-1]
            infinito = voci.get(lemma)
            if lemma.endswith("e") and forma.startswith(radice) and len(forma) > len(radice) and infinito and infinito.toniche:
                tonica_lemma = min(infinito.toniche, key=lambda k: infinito.toniche[k])
                if tonica_lemma < len(radice):
                    voce.aggiungi(tonica_lemma, 5, None, {lemma})
                    continue
            t = tonica_da_desinenza(forma, etichetta)
            if t is not None:
                voce.aggiungi(t, 5, None, {lemma})
        elif etichetta.startswith(("ART", "PRE", "CON", "DET", "PRO", "WH")):
            # Parole grammaticali senza voce: sempre piane (della, nella, sulla, queste).
            lista = nuclei(forma, None)
            if len(lista) == 2:
                voce.aggiungi(prima_forte(forma, lista[0]), 5, None, {lemma})


# ---- Il database ---------------------------------------------------------------

def tieni_varianti(parola, voce, frequenze):
    """Le posizioni dell'accento da tenere: le più sicure, senza quelle di lemmi rarissimi."""
    migliore = min(voce.toniche.values())
    scelte = {t: o for t, o in voce.toniche.items() if o == migliore}
    if len(scelte) == 1:
        return scelte

    def peso(tonica):
        return max((frequenze.get(l, 0) for l in voce.pesi.get(tonica, {""})), default=0)

    massimo = max(peso(t) for t in scelte)
    return {t: o for t, o in scelte.items() if peso(t) >= massimo - SCARTO_MASSIMO_VARIANTE}


def scrivi(uscita, voci, frequenze, conosciute_morphit, offese, statistiche):
    try:
        import os
        os.remove(uscita)
    except OSError:
        pass
    db = sqlite3.connect(uscita)
    db.executescript("""
        PRAGMA page_size = 4096;
        CREATE TABLE parole (
            parola TEXT NOT NULL,
            normalizzata TEXT NOT NULL,
            accentata TEXT NOT NULL,
            sillabe INTEGER NOT NULL,
            tipo INTEGER NOT NULL,
            rima TEXT NOT NULL,
            assonanza TEXT NOT NULL,
            frequenza INTEGER NOT NULL,
            comune INTEGER NOT NULL,
            origine INTEGER NOT NULL
        );
        CREATE TABLE info (chiave TEXT PRIMARY KEY, valore TEXT) WITHOUT ROWID;
    """)
    righe = []
    for parola, voce in voci.items():
        chiave = senza_accenti(parola)
        if not chiave or not re.search("[aeiouy]", chiave):
            continue
        if not voce.toniche:
            statistiche["senza_accento"] += 1
            continue
        # "sonò" non ha la frequenza di "sono": con l'accento scritto vale solo la parola esatta.
        frequenza = frequenze.get(parola, 0 if parola != chiave else frequenze.get(chiave, 0))
        # Le parole mai incontrate restano fuori, tranne i lemmi con pronuncia certa.
        if frequenza == 0 and min(voce.toniche.values()) > 1:
            statistiche["fuori_mai_usate"] += 1
            continue
        offesa = parola in offese or chiave in offese or (voce.ha_sensi and voce.offesa_tutte)
        if not offesa:
            offesa = any(l in offese or senza_accenti(l) in offese for l in voce.lemmi)
        if offesa:
            statistiche["offese_tolte"] += 1
            statistiche.setdefault("elenco_offese", []).append(parola)
        rara = voce.ha_sensi and voce.etichette_tutte
        grammaticale = bool(voce.pos) and voce.pos <= POS_GRAMMATICALI
        comune = 1 if (frequenza >= SOGLIA_COMUNE and not rara and not offesa and not grammaticale
                       and parola not in PAROLE_SOLO_CERCABILI and "'" not in parola) else 0
        varianti = tieni_varianti(parola, voce, frequenze)
        if len(varianti) > 1:
            statistiche["parole_con_varianti"] += 1
        for tonica, origine in varianti.items():
            if tonica >= len(chiave) or chiave[tonica] not in VOCALI:
                statistiche["accento_non_valido"] += 1
                continue
            # La divisione scolastica: Wiktionary divide die-ci in di-e-ci.
            sillabe = conta_sillabe(chiave, tonica)
            accentata = chiave[:tonica] + {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù", "y": "y"}[chiave[tonica]] + chiave[tonica + 1:]
            righe.append((
                parola, chiave, accentata, sillabe, tipo_parola(chiave, tonica),
                chiave_rima(chiave, tonica), chiave_assonanza(chiave, tonica),
                frequenza, comune, origine,
            ))
            statistiche["origine_%d" % origine] += 1
    db.executemany("INSERT INTO parole VALUES (?,?,?,?,?,?,?,?,?,?)", righe)
    db.executescript("""
        CREATE INDEX cerca_parola ON parole (normalizzata);
        CREATE INDEX cerca_rima ON parole (rima, frequenza DESC) WHERE comune = 1;
        CREATE INDEX cerca_assonanza ON parole (assonanza, frequenza DESC) WHERE comune = 1;
    """)
    db.execute("INSERT INTO info VALUES ('versione', '1')")
    db.execute("INSERT INTO info VALUES ('licenza', 'CC BY-SA 4.0')")
    db.commit()
    db.execute("ANALYZE")
    db.execute("VACUUM")
    db.commit()
    statistiche["righe"] = len(righe)
    statistiche["righe_comuni"] = sum(1 for r in righe if r[8])
    db.close()


def controlla_sillabazione(voci, statistiche):
    """Confronta il nostro calcolo delle sillabe con quello di Wiktionary."""
    for parola, voce in voci.items():
        chiave = senza_accenti(parola)
        for tonica, vere in voce.sillabe.items():
            statistiche["sillabe_confrontate"] += 1
            if conta_sillabe(chiave, tonica) != vere:
                statistiche["sillabe_diverse"] += 1


# L'elenco controllato a mano: parola -> chiavi di rima attese. Il generatore si ferma se non torna.
ATTESE = {
    "amore": {"ore"}, "cuore": {"ore"}, "sole": {"ole"}, "mare": {"are"}, "vita": {"ita"}, "notte": {"otte"},
    "tavolo": {"avolo"}, "parlano": {"arlano"}, "pioggia": {"oggia"}, "figlio": {"iglio"}, "camicia": {"icia"},
    "città": {"a"}, "canterò": {"o"}, "perché": {"e"}, "già": {"a"}, "avrà": {"a"}, "più": {"u"}, "qui": {"i"},
    "sono": {"ono"}, "mondo": {"ondo"}, "lavoro": {"oro"}, "medici": {"edici"}, "amavamo": {"amo"},
    "rimasta": {"asta"}, "ancora": {"ora", "ancora"}, "perdono": {"ono", "erdono"}, "subito": {"ubito", "ito"},
    "io": {"io"}, "mio": {"io"}, "mia": {"ia"}, "idea": {"ea"}, "piano": {"ano"}, "credevamo": {"amo"},
    "parlerebbero": {"ebbero"}, "tuo": {"uo"}, "vederlo": {"erlo"}, "parlarne": {"arne"}, "sentirmi": {"irmi"}, "capirlo": {"irlo"}, "credermi": {"edermi"}, "cantando": {"ando"}, "finisco": {"isco"}, "dormono": {"ormono"},
}
SILLABE_ATTESE = {
    "dieci": 2, "auto": 2, "mai": 1, "paura": 3, "aiuto": 3, "paio": 2, "poeta": 3, "mio": 2, "cuore": 2,
    "figlio": 2, "camicia": 3, "costruire": 4, "guida": 2, "qui": 1, "scienza": 2, "tavolo": 3, "chiave": 2, "aereo": 4, "fiume": 2, "giusto": 2, "più": 1, "pioggia": 2,
}


def verifica(uscita):
    db = sqlite3.connect(uscita)
    errori = []
    for parola, attese in ATTESE.items():
        trovate = {r[0] for r in db.execute("SELECT rima FROM parole WHERE parola = ?", (parola,))}
        if trovate != attese:
            errori.append("%s: atteso %s, trovato %s" % (parola, sorted(attese), sorted(trovate)))
    for parola, attese in SILLABE_ATTESE.items():
        trovate = {r[0] for r in db.execute("SELECT sillabe FROM parole WHERE parola = ?", (parola,))}
        if trovate != {attese}:
            errori.append("%s: sillabe attese %d, trovate %s" % (parola, attese, sorted(trovate)))
    db.close()
    return errori


def main():
    argomenti = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argomenti.add_argument("--kaikki", required=True)
    argomenti.add_argument("--morphit", required=True)
    argomenti.add_argument("--wordfreq", required=True)
    argomenti.add_argument("--offese", required=True)
    argomenti.add_argument("--uscita", required=True)
    a = argomenti.parse_args()

    statistiche = collections.Counter()
    frequenze = carica_frequenze(a.wordfreq)
    offese = {r.strip() for r in open(a.offese, encoding="utf8") if r.strip() and not r.startswith("#")}
    voci = {}
    carica_wiktionary(a.kaikki, voci, statistiche)
    per_lemma, conosciute = carica_morphit(a.morphit, voci, statistiche)
    applica_regole(voci, per_lemma, frequenze, statistiche)
    controlla_sillabazione(voci, statistiche)
    scrivi(a.uscita, voci, frequenze, conosciute, offese, statistiche)

    # Quanto sono coperte le parole davvero usate.
    for soglia in (500, 400, 300, 200):
        italiane = [p for p, z in frequenze.items() if z >= soglia and (p in voci or p in conosciute)]
        coperte = [p for p in italiane if p in voci and voci[p].toniche]
        statistiche["copertura_zipf_%d" % soglia] = "%d/%d = %.1f%%" % (len(coperte), len(italiane), 100 * len(coperte) / max(len(italiane), 1))
    elenco = statistiche.pop("elenco_offese", [])
    print(json.dumps(statistiche, indent=1, ensure_ascii=False))
    print("offese tolte:", sorted(elenco))
    errori = verifica(a.uscita)
    print("elenco controllato a mano: %d parole, %d errori" % (len(ATTESE) + len(SILLABE_ATTESE), len(errori)))
    for errore in errori:
        print("  ERRORE", errore)
    if errori:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
