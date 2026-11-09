"""Looks up product photos on Open Food Facts (CC BY-SA, https://openfoodfacts.org) for the catalogue's barcodes
and writes them to javne_slike. Run daily: each run takes the next batch of barcodes it has not looked at yet
(or looked at without success more than 60 days ago) and stays well below Open Food Facts' request limits.

    python slike_off.py --dry-run --max 200     # look up 200 barcodes, print the coverage, write nothing
    python slike_off.py                         # a normal run (needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from skupno import pisi

# Open Food Facts asks for an identifying User-Agent: app name, version and a contact.
UA = "Splitido/1.0 (https://splitido.com; info@splitido.com)"
OFF = "https://world.openfoodfacts.org/api/v2/product/{}.json?fields=code,image_front_small_url"
PRESLEDEK = 0.8  # seconds between two lookups: ~75 a minute, below the 100/min limit for product reads
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


def poisci(ean):
    """(True, url or None) for a definite answer, (False, None) when it should be tried again another day."""
    zahteva = urllib.request.Request(OFF.format(ean), headers={"User-Agent": UA})
    for poskus in range(2):
        try:
            odgovor = json.loads(urllib.request.urlopen(zahteva, timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, None
            if e.code == 429 and poskus == 0:
                time.sleep(60)
                continue
            return False, None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return False, None
        slika = (odgovor.get("product") or {}).get("image_front_small_url")
        if slika and slika.startswith(PREFIKS):
            return True, slika
        return True, None
    return False, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=2500, help="how many barcodes to look up in this run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    kljuc = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not kljuc:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are needed (also for --dry-run: it reads the catalogue)")

    izdelki = preberi(url, kljuc, "javni_izdelki?select=ean&drzava=in.(SI,HR,RS)")
    eani = sorted({r["ean"] for r in izdelki if EAN.match(r["ean"] or "")})
    znani = {r["ean"]: r for r in preberi(url, kljuc, "javne_slike?select=ean,slika,preverjeno")}
    meja = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    na_vrsti = [e for e in eani if e not in znani or (znani[e]["slika"] is None and znani[e]["preverjeno"] < meja)]
    s_sliko = sum(1 for e in eani if e in znani and znani[e]["slika"])
    print(f"{len(eani)} barcodes in the catalogue; {len(znani)} looked up so far ({s_sliko} with a photo); "
          f"{len(na_vrsti)} waiting, taking {min(len(na_vrsti), args.max)}", flush=True)

    vrstice, najdeno, brez = [], 0, 0
    for i, ean in enumerate(na_vrsti[:args.max]):
        dokoncno, slika = poisci(ean)
        if dokoncno:
            vrstice.append({"ean": ean, "slika": slika, "preverjeno": datetime.date.today().isoformat()})
            najdeno += 1 if slika else 0
            brez += 0 if slika else 1
        if len(vrstice) >= 200 and not args.dry_run:
            pisi(url, kljuc, "javne_slike", vrstice, "ean")
            vrstice = []
        if i % 100 == 99:
            print(f"  {i + 1} looked up: {najdeno} with a photo, {brez} without", flush=True)
        time.sleep(PRESLEDEK)
    if vrstice and not args.dry_run:
        pisi(url, kljuc, "javne_slike", vrstice, "ean")
    gledani = najdeno + brez
    delez = f"{100 * najdeno / gledani:.0f}%" if gledani else "-"
    print(f"Done: {najdeno} with a photo, {brez} without ({delez} coverage in this batch)"
          + (" [dry run, nothing written]" if args.dry_run else ""))


if __name__ == "__main__":
    main()
