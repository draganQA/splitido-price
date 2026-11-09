"""Product photos from Open Food Facts (CC BY-SA, https://openfoodfacts.org) for the catalogue's barcodes.

Open Food Facts asks bulk users to take their daily data export instead of asking its servers one product at a
time, so this downloads that one file (about 1.5 GB, gzipped CSV), reads through it once, keeps the front photo of
the barcodes the catalogue has (Slovenia, Croatia, Serbia) and writes them to javne_slike. A barcode with no photo
is written too (slika null), so the coverage can be counted. About ten minutes; run it weekly.

    python slike_off.py --dry-run                 # download, count the coverage, write nothing
    python slike_off.py                           # a normal run (needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)
    python slike_off.py --dump file.csv.gz        # read a file already downloaded
"""
import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import urllib.request

from skupno import pisi

# Open Food Facts asks for an identifying User-Agent: app name, version and a contact.
UA = "Splitido/1.0 (https://splitido.com; info@splitido.com)"
IZVOZ = "https://openfoodfacts-ds.s3.eu-west-3.amazonaws.com/en.openfoodfacts.org.products.csv.gz"
STRAN = 1000
EAN = re.compile(r"^\d{8,14}$")
PREFIKS = "https://images.openfoodfacts.org/"


def preberi(url, kljuc, pot):
    """Every row of a REST query, page by page."""
    vrstice, od = [], 0
    while True:
        zahteva = urllib.request.Request(f"{url}/rest/v1/{pot}", headers={
            "apikey": kljuc, "Authorization": f"Bearer {kljuc}",
            "Range-Unit": "items", "Range": f"{od}-{od + STRAN - 1}"})
        stran = json.loads(urllib.request.urlopen(zahteva, timeout=120).read())
        vrstice += stran
        if len(stran) < STRAN:
            return vrstice
        od += STRAN


def brez_nicel(ean):
    """Open Food Facts keeps some barcodes with leading zeros and some without: compare without them."""
    return ean.lstrip("0")


def vrstice_izvoza(pot):
    """The rows of the export (a local file or the download), as lists of fields."""
    if pot:
        tok = gzip.open(pot, "rb")
    else:
        zahteva = urllib.request.Request(IZVOZ, headers={"User-Agent": UA})
        tok = gzip.GzipFile(fileobj=urllib.request.urlopen(zahteva, timeout=120))
    csv.field_size_limit(10 ** 9)
    besedilo = io.TextIOWrapper(tok, encoding="utf-8", errors="replace", newline="")
    return csv.reader(besedilo, delimiter="\t", quoting=csv.QUOTE_NONE)


def poisci_slike(iskani, pot):
    """{barcode without leading zeros: photo url} for the wanted ones that have a front photo."""
    vrstice = vrstice_izvoza(pot)
    glava = next(vrstice)
    i_koda, i_slika = glava.index("code"), glava.index("image_small_url")
    najdeno, pregledano = {}, 0
    for vrstica in vrstice:
        pregledano += 1
        if len(vrstica) <= i_slika:
            continue
        koda = brez_nicel(vrstica[i_koda])
        slika = vrstica[i_slika]
        if koda in iskani and slika.startswith(PREFIKS) and "/front_" in slika:
            najdeno[koda] = slika
        if pregledano % 500_000 == 0:
            print(f"  {pregledano} products read, {len(najdeno)} photos found", flush=True)
    print(f"{pregledano} products in the export, {len(najdeno)} photos found for our barcodes", flush=True)
    return najdeno


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", help="a downloaded export (en.openfoodfacts.org.products.csv.gz) instead of downloading it")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    kljuc = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not kljuc:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are needed (also for --dry-run: it reads the catalogue)")

    izdelki = preberi(url, kljuc, "javni_izdelki?select=ean&drzava=in.(SI,HR,RS)")
    eani = sorted({r["ean"] for r in izdelki if EAN.match(r["ean"] or "")})
    print(f"{len(eani)} barcodes in the catalogue", flush=True)
    slike = poisci_slike({brez_nicel(e) for e in eani}, args.dump)

    danes = datetime.date.today().isoformat()
    vrstice = [{"ean": e, "slika": slike.get(brez_nicel(e)), "preverjeno": danes} for e in eani]
    z_sliko = sum(1 for v in vrstice if v["slika"])
    delez = f"{100 * z_sliko / len(vrstice):.0f}%" if vrstice else "-"
    print(f"Done: {z_sliko} with a photo, {len(vrstice) - z_sliko} without ({delez} coverage)"
          + (" [dry run, nothing written]" if args.dry_run else ""))
    if vrstice and not args.dry_run:
        pisi(url, kljuc, "javne_slike", vrstice, "ean")


if __name__ == "__main__":
    main()
