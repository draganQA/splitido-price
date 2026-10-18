"""Matching the same product across chains by name, for the countries whose price lists have no barcode (MK, BG).

A name is cleaned (script, accents, punctuation), the pack size and the fat are read out of it as numbers, and what
is left is a set of words, each worth more the rarer it is. Two products of different chains are the same product
when their sizes agree and enough of their (weighted) words are shared, and each is the other's best match.
Nothing here is certain: a group carries the score of its weakest link.
"""
import math
import re
from collections import defaultdict

# Cyrillic -> Latin, the same letters as the database's javni_kljuc() uses
LAT = str.maketrans("абвгдѓежзѕијклљмнњопрстќуфхцчџшђћйъяюыэёь", "abvgdgezzzijkllmnnoprstkufhcccsdcjauaieei")
DIAKRITIKI = str.maketrans("čćšžđàáâãäåèéêëìíîïòóôõöùúûüñçß", "ccszdaaaaaaeeeeiiiiooooouuuuncs")
ENOTE = {"kg": ("g", 1000), "g": ("g", 1), "gr": ("g", 1), "gm": ("g", 1), "mg": ("g", 0.001),
         "l": ("ml", 1000), "lt": ("ml", 1000), "ltr": ("ml", 1000), "ml": ("ml", 1), "dl": ("ml", 100), "cl": ("ml", 10)}
VELIKOST = re.compile(r"(?:(\d+)\s*[x*]\s*)?(\d+(?:\.\d+)?)\s*(kg|gr|gm|mg|g|lt|ltr|l|ml|dl|cl)\b")
MASCOBA = re.compile(r"(\d+(?:\.\d+)?)\s*%")
BREZ_POMEN = {"kom", "pcs", "pak", "pk", "pakuvanje", "pakovanje", "vrecka", "ml", "mm", "m"}


def normaliziraj(ime):
    """-> (words, size in g or ml or None, unit, number in a multipack, fat or None)"""
    s = ime.lower().translate(LAT).translate(DIAKRITIKI)
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    s = re.sub(r"[^a-z0-9.%*]+", " ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)
    mascoba = None
    m = MASCOBA.search(s)
    if m:
        mascoba = float(m.group(1))
        s = s.replace(m.group(0), " ")
    velikost = enota = None
    paketov = 1
    vse = list(VELIKOST.finditer(s))
    if vse:
        m = vse[-1]
        baza, faktor = ENOTE[m.group(3)]
        paketov = int(m.group(1) or 1)
        velikost = round(float(m.group(2)) * faktor * paketov, 3)
        enota = baza
        s = s.replace(m.group(0), " ")
    besede = []
    for b in s.replace("*", " ").split():
        if b in BREZ_POMEN or b == ".":
            continue
        besede.append(b[:5] if len(b) > 5 else b)   # a stem: endings differ between chains
    return besede, velikost, enota, paketov, mascoba


def sorodno(a, b, toleranca=0.03):
    """May two normalised products be the same at all (size, fat and pack count agree)?"""
    _, va, ea, pa, ma = a
    _, vb, eb, pb, mb = b
    if va is not None and vb is not None:
        if ea != eb or abs(va - vb) > toleranca * max(va, vb):
            return False
    if ma is not None and mb is not None and abs(ma - mb) > 0.05:
        return False
    return True


def poisci_skupine(izdelki, prag=0.55, zanesljivo=0.75):
    """izdelki: list of (chain, name, price). -> list of groups, each a list of indexes into izdelki, with a score."""
    n = len(izdelki)
    norm = [normaliziraj(i[1]) for i in izdelki]
    df = defaultdict(int)
    for besede, *_ in norm:
        for b in set(besede):
            df[b] += 1
    idf = {b: math.log((n + 1) / (c + 0.5)) for b, c in df.items()}
    kazalo = defaultdict(list)
    for i, (besede, *_) in enumerate(norm):
        for b in set(besede):
            if df[b] <= max(40, n * 0.01):    # a word half the catalogue has finds nothing
                kazalo[b].append(i)
    najboljsi = {}      # (i, chain of the other) -> (score, j)
    for i, (besede_i, *rest_i) in enumerate(norm):
        mi = set(besede_i)
        if not mi:
            continue
        kand = set()
        for b in mi:
            for j in kazalo.get(b, ()):
                if izdelki[j][0] != izdelki[i][0]:
                    kand.add(j)
        for j in kand:
            if not sorodno(norm[i], norm[j]):
                continue
            mj = set(norm[j][0])
            skupaj = sum(idf[b] for b in mi & mj)
            unija = sum(idf[b] for b in mi | mj)
            ocena = skupaj / unija if unija else 0
            # each side has a rare word the other lacks (two flavours, two brands): probably two products
            ednina_i = [b for b in mi - mj if idf[b] >= 5.5]
            ednina_j = [b for b in mj - mi if idf[b] >= 5.5]
            if ednina_i and ednina_j:
                ocena *= 0.75
            # a size on one side only is weaker evidence
            if (norm[i][1] is None) != (norm[j][1] is None):
                ocena *= 0.85
            if ocena < prag:
                continue
            k = (i, izdelki[j][0])
            if k not in najboljsi or ocena > najboljsi[k][0]:
                najboljsi[k] = (ocena, j)
    # mutual best matches become edges
    robovi = []
    for (i, veriga_j), (ocena, j) in najboljsi.items():
        nazaj = najboljsi.get((j, izdelki[i][0]))
        if nazaj and nazaj[1] == i and i < j:
            robovi.append((ocena, i, j))
    robovi.sort(reverse=True)
    starsi = list(range(n))
    skupina_verige = {i: {izdelki[i][0]} for i in range(n)}
    min_ocena = {i: 1.0 for i in range(n)}

    def koren(x):
        while starsi[x] != x:
            starsi[x] = starsi[starsi[x]]
            x = starsi[x]
        return x
    for ocena, i, j in robovi:
        a, b = koren(i), koren(j)
        if a == b or skupina_verige[a] & skupina_verige[b]:
            continue      # never two products of one chain in a group
        starsi[b] = a
        skupina_verige[a] |= skupina_verige[b]
        min_ocena[a] = min(min_ocena[a], min_ocena[b], ocena)
    skupine = defaultdict(list)
    for i in range(n):
        skupine[koren(i)].append(i)
    return [(sorted(c), round(min_ocena[k], 2)) for k, c in skupine.items() if len(c) >= 2]


PAKET = re.compile(r"(?<![\d.])(\d+)\s*/\s*1(?!\d)|(?<![\d.])1\s*/\s*(\d+)(?!\d)")


def razlicen_paket(imena):
    """Do the names state different pack counts ("3/1" and "12/1")?"""
    p = [{int(a or b) for a, b in PAKET.findall(i)} for i in imena]
    p = [x for x in p if x]
    return len(p) >= 2 and any(x != p[0] for x in p)


def naredi_skupine(izdelki, odobreni=(), zanesljivo=0.75):
    """izdelki: list of (id, chain, name) or (id, chain, name, price). odobreni: groups (lists of ids) a person has looked at and found right,
    for the matches the rules are less sure of. -> list of (id, group id, score, confirmed)."""
    import hashlib
    videni = {}
    cene = {}
    for id_, veriga, ime, *cena in izdelki:
        videni.setdefault(id_, (veriga, ime))
        if cena and cena[0]:
            cene.setdefault(id_, cena[0])
    ids = list(videni)
    vhod = [(videni[i][0], videni[i][1], 0) for i in ids]
    izhod = []
    uporabljen = set()

    def skupina_id(clani):
        return "g" + hashlib.sha1("|".join(sorted(clani)).encode()).hexdigest()[:11]

    for clani, ocena in poisci_skupine(vhod):
        # a little less sure is enough when every product names its size (and the sizes agree: poisci_skupine checks that);
        # of the matches looked at by a person, all of those were right
        z_velikostjo = ocena >= 0.65 and all(normaliziraj(vhod[i][1])[1] is not None for i in clani)
        # and from 0.70 when the prices of the products are close (looked at by a person: 157 of 168 were right)
        c_ = [ids[i] for i in clani]
        pr = [cene[x] for x in c_ if x in cene]
        blizu_cene = ocena >= 0.70 and len(pr) == len(c_) and max(pr) / min(pr) <= 1.4 and not razlicen_paket(videni[x][1] for x in c_)
        if ocena >= zanesljivo or z_velikostjo or blizu_cene:
            c = [ids[i] for i in clani]
            sid = skupina_id(c)
            izhod.extend((x, sid, ocena, False) for x in c)
            uporabljen.update(c)
    for skupina in odobreni:
        c = [x for x in skupina if x in videni and x not in uporabljen]
        verige = {videni[x][0] for x in c}
        if len(c) >= 2 and len(verige) == len(c):      # one product of each chain at most
            sid = skupina_id(c)
            izhod.extend((x, sid, 1.0, True) for x in c)
            uporabljen.update(c)
    return izhod
