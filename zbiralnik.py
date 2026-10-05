#!/usr/bin/env python3
"""
Collects public shop prices for Splitido's "prices in the country" and writes
them to Supabase. Only the standard library, so the workflow needs no install.

Croatia: since 15 May 2025 every chain publishes its price lists every day in a
machine-readable form. cijene.dev gathers them into one ZIP a day (a CSV
catalogue and a CSV of prices per chain); this reads that ZIP and keeps ONE
current price per chain and product: the price (the most common one across the
chain's stores), whether the chain marks it as a special offer (special_price is
set; the price itself is then already the lowered one), the price before the
offer (the lowest price of the 30 days before, else the anchor price of
2 May 2025) and the lowest price of the last 30 days. A product is kept when
two or more chains sell it (so there is something to compare) or when it is on
offer; the rest would only fill the database.

  python zbiralnik.py --dry-run               # download, boil down, print numbers
  python zbiralnik.py --archive file.zip --dry-run
  python zbiralnik.py                         # and write to Supabase

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to write. The key is the
service role one: keep it in the repository's secrets, never in a file.
"""
import argparse
import collections
import csv
import datetime
import io
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

from skupno import pisi, pocisti

LIST_URL = "https://api.cijene.dev/v0/list"
DRZAVA = "HR"
# Chains in this order win when the same barcode is named differently.
VERIGE = {
    "konzum": "Konzum", "lidl": "Lidl", "spar": "Spar", "plodine": "Plodine", "kaufland": "Kaufland",
    "studenac": "Studenac", "tommy": "Tommy", "eurospin": "Eurospin", "dm": "dm", "ribola": "Ribola",
    "metro": "Metro", "ntl": "NTL", "ktc": "KTC", "trgocentar": "Trgocentar", "zabac": "Žabac",
    "vrutak": "Vrutak", "roto": "Roto",
}


def decimal(s):
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        v = round(float(s), 2)
    except ValueError:
        return None
    return v if 0 <= v < 100000 else None


def najnovejsa_arhiva(pot):
    with urllib.request.urlopen(LIST_URL, timeout=60) as r:
        arhive = json.load(r)["archives"]
    arhive.sort(key=lambda a: a["date"], reverse=True)
    a = arhive[0]
    print(f"Archive {a['date']} ({a['size'] // 1_000_000} MB)", flush=True)
    urllib.request.urlretrieve(a["url"], pot)
    return a["date"]


def beri_csv(zf, ime):
    with zf.open(ime) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))


def modus(stevec):
    return stevec.most_common(1)[0][0] if stevec else None


def agregiraj(pot_zip):
    """-> (izdelki {ean: row}, cene [row], stevilo_po_verigi)"""
    izdelki = {}
    cene = {}
    stevilo = {}
    with zipfile.ZipFile(pot_zip) as zf:
        imena = set(zf.namelist())
        for koda, veriga in VERIGE.items():
            if f"{koda}/products.csv" not in imena or f"{koda}/prices.csv" not in imena:
                continue
            izdelek = {}
            for p in beri_csv(zf, f"{koda}/products.csv"):
                izdelek[p["product_id"]] = p
            redne = collections.defaultdict(collections.Counter)
            akcijske = collections.Counter()
            referencne = collections.defaultdict(collections.Counter)
            sidrene = collections.defaultdict(collections.Counter)
            najnizje = {}
            for c in beri_csv(zf, f"{koda}/prices.csv"):
                pid = c["product_id"]
                cena = decimal(c.get("price"))
                if not cena or pid not in izdelek:
                    continue
                redne[pid][cena] += 1
                # 0.00 means "not given", in some chains' files.
                if decimal(c.get("special_price")):
                    akcijske[pid] += 1
                n = decimal(c.get("best_price_30"))
                if n:
                    if pid not in najnizje or n < najnizje[pid]:
                        najnizje[pid] = n
                    if n > cena:
                        referencne[pid][n] += 1
                a = decimal(c.get("anchor_price"))
                if a:
                    sidrene[pid][a] += 1
            n_verige = 0
            for pid, stevci in redne.items():
                p = izdelek[pid]
                ean = (p.get("barcode") or "").strip()
                ime = " ".join((p.get("name") or "").split())[:200]
                if not ean or not ime:
                    continue
                cena = modus(stevci)
                vrstic = sum(stevci.values())
                akcija = akcijske[pid] / vrstic > 0.3
                sidrena = modus(sidrene[pid])
                referencna = None
                if akcija:
                    referencna = modus(referencne[pid]) or (sidrena if sidrena and sidrena > cena else None)
                vrstica = {
                    "drzava": DRZAVA, "ean": ean, "veriga": veriga, "cena": cena, "akcija": akcija,
                    "referencna": referencna, "najnizja30": najnizje.get(pid), "sidrena": sidrena,
                }
                prejsnja = cene.get((ean, veriga))
                if prejsnja is None or cena < prejsnja["cena"]:
                    cene[(ean, veriga)] = vrstica
                if ean not in izdelki:
                    izdelki[ean] = {
                        "drzava": DRZAVA, "ean": ean, "ime": ime,
                        "znamka": " ".join((p.get("brand") or "").split())[:80] or None,
                        "kolicina": (p.get("unit") or "").strip()[:40] or None,
                        "kategorija": (p.get("category") or "").strip()[:80] or None,
                    }
                n_verige += 1
            stevilo[veriga] = n_verige

    # Keep what can be compared (two or more chains) or is on offer.
    verig_po_eanu = collections.Counter(ean for ean, _ in cene)
    akcijski = {ean for (ean, _), c in cene.items() if c["akcija"]}
    obdrzi = {ean for ean, n in verig_po_eanu.items() if (n >= 2 and ":" not in ean) or ean in akcijski}
    izdelki = {e: i for e, i in izdelki.items() if e in obdrzi}
    cene = [c for (ean, _), c in cene.items() if ean in obdrzi]
    return izdelki, cene, stevilo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", help="a ZIP already downloaded (otherwise the newest is downloaded)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="also write the rows as JSON lines to this folder")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        pot = args.archive or os.path.join(tmp, "arhiva.zip")
        datum = datetime.date.today().isoformat() if args.archive else najnovejsa_arhiva(pot)
        izdelki, cene, stevilo = agregiraj(pot)

    for v, n in stevilo.items():
        print(f"  {v}: {n} products")
    print(f"Catalogue: {len(izdelki)} products, prices: {len(cene)}", flush=True)
    if len(izdelki) < 20000 or len(cene) < 50000:
        sys.exit("Far fewer products than usual: not writing (a chain's format probably changed).")
    for c in cene:
        c["posodobljeno"] = datum
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        for ime, vrstice in (("javni_izdelki", list(izdelki.values())), ("javne_cene", cene)):
            with open(os.path.join(args.out, ime + ".jsonl"), "w") as f:
                for v in vrstice:
                    f.write(json.dumps(v, ensure_ascii=False) + "\n")
    if args.dry_run:
        return

    url = os.environ["SUPABASE_URL"].rstrip("/")
    kljuc = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    pisi(url, kljuc, "javni_izdelki", list(izdelki.values()), "drzava,ean")
    pisi(url, kljuc, "javne_cene", cene, "drzava,ean,veriga")
    pocisti(url, kljuc, datum, DRZAVA)
    print("Done.")


if __name__ == "__main__":
    main()
