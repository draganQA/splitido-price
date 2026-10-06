#!/usr/bin/env python3
"""
Public shop prices for Serbia, from the chains' own price lists.

Under the Regulation on special conditions for trade, the big chains publish
their price lists every week as CSV files on the Open Data Portal (data.gov.rs),
all in one format, with the product's barcode: "Barkod proizvoda", name, brand,
regular price ("Redovna cena"), price on offer ("Snižena cena") with the dates
the offer lasts, and the kind of list. Only the current lists are used
(VRSTA_CENOVNIKA = VAZECI_CENOVNIK); the others are monthly snapshots. Only the
standard library.

A chain's file is read from where the chain keeps it (Lidl, Maxi, Idea, Univerexport,
Gomex) or from the portal. Where a file holds several shop formats of a company
(Maxi, Maxi Online, Mega Maxi, ...; Idea, Mercator, Roda), one format stands for
the chain. A product is kept when two or more chains sell it or when it is on offer.

  python zbiralnik_rs.py --dry-run          # read, print the numbers
  python zbiralnik_rs.py                    # and write to Supabase
"""
import argparse
import collections
import csv
import datetime
import io
import json
import os
import sys
import time
import urllib.request

from skupno import decimal, pisi, pocisti

DRZAVA = "RS"
UA = "Mozilla/5.0 (compatible; SplitidoPriceBot/1.0; +https://github.com/draganQA/splitido-price)"
PORTAL = "https://data.gov.rs/sr/datasets/r/"

# (the file, the formats of the file that count -> the chain's name, at least this many rows)
VIRI = [
    ("https://tsmdelhaizeserbia.delhaize.rs/PublicDoc/cene_proizvoda_Delhaize.csv", {"maxi": "Maxi"}, 3000),
    ("https://ideacenovnici.blob.core.windows.net/cenovnici/drzavni/cene_proizvoda_ideamarketi.csv",
     {"idea": "Idea", "mercator": "Mercator", "roda": "Roda"}, 15000),
    ("https://ucloud.univerexport.rs/opendata/univer.csv", {"univerexport - m": "Univerexport"}, 8000),
    ("https://kompanija.lidl.rs/content/download/165294/fileupload/cene_proizvoda_Lidl.csv", {"*": "Lidl"}, 1500),
    ("https://aswcene.gomex.rs/upload/cene_proizvoda_gomex.csv", {"*": "Gomex"}, 1000),
    (PORTAL + "225fff85-9efb-424f-b70a-48375b07c570", {"*": "Vero"}, 7000),
    (PORTAL + "7788bebf-4b63-4ecc-894e-cc15c772e4aa", {"*": "Fortuna"}, 4000),
    (PORTAL + "90465a2b-ad9f-4c42-b727-4bbc2bcbd7bb", {"*": "Aman"}, 3500),
    (PORTAL + "c1d29f13-5087-4b60-81d4-22614cb0f90e", {"*": "Metro"}, 4000),
]


def prenesi(url, poskusov=3):
    for poskus in range(poskusov):
        try:
            zahteva = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(zahteva, timeout=180) as r:
                return r.read().decode("utf-8-sig", "replace").replace("\x00", "")
        except Exception:
            if poskus == poskusov - 1:
                raise
            time.sleep(4 * (poskus + 1))


def datum(s):
    try:
        return datetime.datetime.strptime((s or "").strip(), "%d-%m-%Y").date()
    except ValueError:
        return None


def stolpec(vrstica, predpona):
    for k, v in vrstica.items():
        if k and k.strip().strip('"').lower().startswith(predpona):
            return (v or "").strip()
    return ""


def preberi(besedilo, formati, danes):
    """The products of one file as (ean, name, brand, category, chain, price, on offer, regular price)."""
    prva = besedilo.split("\n", 1)[0]
    locilo = ";" if prva.count(";") > prva.count(",") else ","
    izhod = []
    for v in csv.DictReader(io.StringIO(besedilo), delimiter=locilo):
        if stolpec(v, "vrsta") not in ("", "VAZECI_CENOVNIK"):
            continue
        oblika = stolpec(v, "naziv trgov").lower().replace("–", "-")
        veriga = formati.get(oblika) or formati.get("*")
        if not veriga:
            continue
        ean = stolpec(v, "barkod")
        ime = " ".join(stolpec(v, "naziv proizvoda").split())[:200]
        redovna = decimal(stolpec(v, "redovna"))
        if not ean.isdigit() or not 8 <= len(ean) <= 14 or not ime or not redovna:
            continue
        cena, akcija = redovna, False
        znizana = decimal(stolpec(v, "sni"))
        od, do = datum(stolpec(v, "datum po")), datum(stolpec(v, "datum kr"))
        veljavna = (od is None or od <= danes) and (do is None or danes <= do)
        if znizana and znizana < redovna and veljavna:
            cena, akcija = znizana, True
        izhod.append((ean, ime, " ".join(stolpec(v, "robna").split())[:80] or None,
                      " ".join(stolpec(v, "naziv kat").split())[:80] or None, veriga, cena, akcija, redovna))
    return izhod


def zberi():
    danes = datetime.date.today()
    izdelki, cene, stevilo, napake = {}, {}, {}, []
    for url, formati, najmanj in VIRI:
        ime = "/".join(sorted(set(formati.values())))
        try:
            vrstice = preberi(prenesi(url), formati, danes)
        except Exception as e:  # a chain that doesn't answer must not stop the others
            napake.append(f"{ime}: {e}")
            continue
        if len(vrstice) < najmanj:
            napake.append(f"{ime}: only {len(vrstice)} rows (at least {najmanj} expected): its file has probably changed")
            continue
        for ean, naziv, znamka, kategorija, veriga, cena, akcija, redovna in vrstice:
            stevilo[veriga] = stevilo.get(veriga, 0) + 1
            prejsnja = cene.get((ean, veriga))
            if prejsnja is None or cena < prejsnja["cena"]:
                cene[(ean, veriga)] = {
                    "drzava": DRZAVA, "ean": ean, "veriga": veriga, "cena": cena, "akcija": akcija,
                    "referencna": redovna if akcija else None, "najnizja30": None, "sidrena": None,
                }
            if ean not in izdelki:
                izdelki[ean] = {"drzava": DRZAVA, "ean": ean, "ime": naziv, "znamka": znamka, "kolicina": None, "kategorija": kategorija}
    # Keep what can be compared (two or more chains) or is on offer.
    verig_po_eanu = collections.Counter(ean for ean, _ in cene)
    akcijski = {ean for (ean, _), c in cene.items() if c["akcija"]}
    obdrzi = {ean for ean, n in verig_po_eanu.items() if n >= 2 or ean in akcijski}
    return ({e: i for e, i in izdelki.items() if e in obdrzi}, [c for (ean, _), c in cene.items() if ean in obdrzi], stevilo, napake)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="also write the rows as JSON lines to this folder")
    args = ap.parse_args()

    izdelki, cene, stevilo, napake = zberi()
    for v, n in stevilo.items():
        print(f"  {v}: {n} rows")
    for n in napake:
        print("  !", n)
    print(f"Catalogue: {len(izdelki)} products, prices: {len(cene)}", flush=True)
    if len(izdelki) < 10000 or len(cene) < 30000:
        sys.exit("Far fewer products than usual: not writing (a file's format probably changed).")
    danes = datetime.date.today().isoformat()
    for c in cene:
        c["posodobljeno"] = danes
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
    pocisti(url, kljuc, danes, DRZAVA)
    print("Done.")
    if napake:
        sys.exit("Some chains failed: " + "; ".join(napake))


if __name__ == "__main__":
    main()
