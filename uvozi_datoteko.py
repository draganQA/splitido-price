#!/usr/bin/env python3
"""
Imports a prepared file of one chain's prices into Supabase (the same tables and the same writing as the collectors).

For a chain whose pages cannot be read from the cloud machines that run the collectors: the file is made somewhere
else, committed under data/, and this reads it. The file is JSON (or .json.gz):

  {"datum": "2026-10-16", "veriga": "Mercator", "izdelki": [javni_izdelki rows], "cene": [javne_cene rows]}

Nothing is written unless the file passes the checks (one country, one chain, sane prices, enough rows).

  python uvozi_datoteko.py data/mercator.json.gz --dry-run
  python uvozi_datoteko.py data/mercator.json.gz
"""
import argparse
import datetime
import gzip
import json
import os
import sys

from skupno import pisi, pocisti

IZDELEK = {"drzava", "ean", "ime", "znamka", "kolicina", "kategorija"}
CENA = {"drzava", "ean", "veriga", "cena", "akcija", "referencna", "najnizja30", "sidrena", "posodobljeno"}


def preberi(pot):
    if not os.path.normpath(pot).startswith("data" + os.sep):
        sys.exit("The file must be under data/.")
    odpri = gzip.open if pot.endswith(".gz") else open
    with odpri(pot, "rt", encoding="utf-8") as f:
        return json.load(f)


def preveri(d, najmanj):
    napake = []
    veriga, datum = d.get("veriga"), d.get("datum")
    izdelki, cene = d.get("izdelki") or [], d.get("cene") or []
    try:
        dan = datetime.date.fromisoformat(datum)
        if (datetime.date.today() - dan).days > 14 or dan > datetime.date.today():
            napake.append(f"the date {datum} is not within the last two weeks")
    except (TypeError, ValueError):
        napake.append(f"bad date {datum!r}")
    if not veriga or not isinstance(veriga, str):
        napake.append("no chain name")
    if len(cene) < najmanj or len(izdelki) < najmanj:
        napake.append(f"only {len(izdelki)} products / {len(cene)} prices (at least {najmanj} expected)")
    eani = {p.get("ean") for p in izdelki}
    for p in izdelki:
        if set(p) != IZDELEK or not p["ean"] or not p["ime"] or p["drzava"] != "SI":
            napake.append(f"bad product row: {str(p)[:100]}"); break
    videni = set()
    for c in cene:
        if set(c) != CENA or c["drzava"] != "SI" or c["veriga"] != veriga or c["ean"] not in eani:
            napake.append(f"bad price row: {str(c)[:100]}"); break
        if not (0 < float(c["cena"]) < 10000) or (c["referencna"] is not None and not float(c["referencna"]) > float(c["cena"])):
            napake.append(f"odd price: {str(c)[:100]}"); break
        if c["posodobljeno"] != datum:
            napake.append("a price row has another date than the file"); break
        k = (c["ean"], c["veriga"])
        if k in videni:
            napake.append(f"the same product twice: {k}"); break
        videni.add(k)
    return napake


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("datoteka")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--najmanj", type=int, default=1000)
    ap.add_argument("--dni", type=int, default=10, help="days after which the chain's older prices are removed")
    args = ap.parse_args()
    d = preberi(args.datoteka)
    napake = preveri(d, args.najmanj)
    print(f"{d.get('veriga')}: {len(d.get('izdelki') or [])} products, {len(d.get('cene') or [])} prices, date {d.get('datum')}")
    if napake:
        sys.exit("Not written: " + "; ".join(napake))
    if args.dry_run:
        print("The file is fine (dry run).")
        return
    url = os.environ["SUPABASE_URL"].rstrip("/")
    kljuc = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    pisi(url, kljuc, "javni_izdelki", d["izdelki"], "drzava,ean")
    pisi(url, kljuc, "javne_cene", d["cene"], "drzava,ean,veriga")
    pocisti(url, kljuc, d["datum"], "SI", veriga=d["veriga"], dni=args.dni)
    print("Done.")


if __name__ == "__main__":
    main()
