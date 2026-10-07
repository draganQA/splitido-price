#!/usr/bin/env python3
"""
Public shop prices for Bulgaria, from the Consumer Protection Commission's portal kolkostruva.bg.

Since the euro was introduced the retailers have to report their prices of the consumer basket every
day, and the commission publishes them as open data: one ZIP a day
(https://kolkostruva.bg/opendata_files/YYYY-MM-DD.zip) with one CSV per retailer, one row per shop and
product (place, shop, product, product code, category, retail price, promotional price; prices in euro).
The product code is the chain's own, not a barcode, so (like Macedonia) a product is identified by
chain + code and matched to the flat's products by name. Only the standard library.

Per chain and product:
  - the price: the most common one across the chain's shops (the lowest when tied);
  - an offer: the promotional price is set and under the retail price (the price is then the lowered
    one, `referencna` the retail price).

Only the chains below are read (the big national ones); a chain with far fewer products than usual is
left out for the day and the others are still written.

  python zbiralnik_bg.py --dry-run                 # download today's (or the latest) archive, print the numbers
  python zbiralnik_bg.py --archive 2026-10-07.zip  # a local file
  python zbiralnik_bg.py                           # and write to Supabase
"""
import argparse
import collections
import csv
import datetime
import hashlib
import io
import os
import sys
import urllib.error
import urllib.request
import zipfile

from skupno import decimal, pisi, pocisti

DRZAVA = "BG"
UA = "Mozilla/5.0 (compatible; SplitidoPriceBot/1.0; +https://github.com/draganQA/splitido-price)"
ARHIV = "https://kolkostruva.bg/opendata_files/{datum}.zip"

# the start of a retailer's file name -> (chain as shown, at least this many products)
VERIGE = [
    ("Билла", "Billa", 800),
    ("Кауфланд", "Kaufland", 1500),
    ("Лидл", "Lidl", 400),
    ("T Market", "T-Market", 400),
    ("ФАНТАСТИКО", "Fantastico", 1000),
    ("Метро", "Metro", 1000),
    ("Бурлекс", "CBA", 800),
    ("МИНИМАРТ", "Minimart", 300),
    ("Lilly", "Lilly", 400),
    ("ДМ България", "dm", 400),
]


def prenesi_arhiv(poti_dni=5):
    """The newest archive that exists: today's, else one of the days before."""
    danes = datetime.date.today()
    for nazaj in range(poti_dni):
        datum = (danes - datetime.timedelta(days=nazaj)).isoformat()
        zahteva = urllib.request.Request(ARHIV.format(datum=datum), headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(zahteva, timeout=300) as r:
                return r.read(), datum
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    raise RuntimeError("no archive for the last %d days" % poti_dni)


def id_izdelka(veriga, koda):
    return f"{veriga.lower()}:{hashlib.sha1(koda.encode()).hexdigest()[:12]}"


def preberi_verigo(csv_besedilo):
    """One retailer's CSV -> {code: (name, price, offer, regular price)}."""
    cene = collections.defaultdict(collections.Counter)   # code -> {(price, offer, regular): shops}
    imena = collections.defaultdict(collections.Counter)  # code -> {name: shops}
    bralnik = csv.reader(io.StringIO(csv_besedilo))
    next(bralnik, None)
    for v in bralnik:
        if len(v) < 6:
            continue
        ime, koda = " ".join(v[2].split())[:200], v[3].strip()
        redna = decimal(v[5])
        akcijska = decimal(v[6]) if len(v) > 6 else None
        if not ime or not koda or not redna or redna <= 0:
            continue
        if akcijska and 0 < akcijska < redna:
            cene[koda][(akcijska, True, redna)] += 1
        else:
            cene[koda][(redna, False, None)] += 1
        imena[koda][ime] += 1
    izhod = {}
    for koda, stetje in cene.items():
        (cena, akcija, redna), _ = min(stetje.items(), key=lambda kv: (-kv[1], kv[0][0]))
        ime = imena[koda].most_common(1)[0][0]
        izhod[koda] = (ime, cena, akcija, redna)
    return izhod


def zberi(zip_bajti):
    izdelki, cene, stevilo, napake = {}, {}, {}, []
    z = zipfile.ZipFile(io.BytesIO(zip_bajti))
    datoteke = collections.defaultdict(list)
    for ime in z.namelist():
        for predpona, veriga, _ in VERIGE:
            if ime.startswith(predpona):
                datoteke[veriga].append(ime)
    for predpona, veriga, najmanj in VERIGE:
        if not datoteke[veriga]:
            napake.append(f"{veriga}: no file in the archive")
            continue
        seznam = {}
        for ime in datoteke[veriga]:   # a chain may report in several files (franchisees)
            for koda, p in preberi_verigo(z.read(ime).decode("utf-8", "replace")).items():
                seznam.setdefault(koda, p)
        if len(seznam) < najmanj:
            napake.append(f"{veriga}: only {len(seznam)} products (at least {najmanj} expected): the file has probably changed")
            continue
        stevilo[veriga] = len(seznam)
        for koda, (ime, cena, akcija, redna) in seznam.items():
            ean = id_izdelka(veriga, koda)
            cene[(ean, veriga)] = {
                "drzava": DRZAVA, "ean": ean, "veriga": veriga, "cena": cena, "akcija": akcija,
                "referencna": redna if akcija else None, "najnizja30": None, "sidrena": None,
            }
            izdelki[ean] = {"drzava": DRZAVA, "ean": ean, "ime": ime, "znamka": None, "kolicina": None, "kategorija": None}
    return izdelki, list(cene.values()), stevilo, napake


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--archive", help="a local ZIP instead of downloading")
    ap.add_argument("--out", help="write the rows as SQL inserts to this file (for tests)")
    args = ap.parse_args()

    if args.archive:
        with open(args.archive, "rb") as f:
            bajti = f.read()
        datum = datetime.date.today().isoformat()
    else:
        bajti, datum = prenesi_arhiv()
    print(f"Archive of {datum}: {len(bajti) // 1024} KB")

    izdelki, cene, stevilo, napake = zberi(bajti)
    for v, n in stevilo.items():
        print(f"  {v}: {n} products")
    for n in napake:
        print("  !", n)
    print(f"Catalogue: {len(izdelki)} products, prices: {len(cene)}", flush=True)
    if not cene:
        sys.exit("Nothing read.")

    datum_cen = datetime.date.today().isoformat()
    for c in cene:
        c["posodobljeno"] = datum_cen
    if args.out:
        import json
        with open(args.out, "w") as f:
            json.dump({"izdelki": list(izdelki.values()), "cene": cene}, f)
    if args.dry_run:
        return

    url = os.environ["SUPABASE_URL"].rstrip("/")
    kljuc = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    pisi(url, kljuc, "javni_izdelki", list(izdelki.values()), "drzava,ean")
    pisi(url, kljuc, "javne_cene", cene, "drzava,ean,veriga")
    pocisti(url, kljuc, datum_cen, DRZAVA)
    print("Done.")
    if napake:
        sys.exit("Some chains failed: " + "; ".join(napake))


if __name__ == "__main__":
    main()
