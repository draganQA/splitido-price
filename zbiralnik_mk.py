#!/usr/bin/env python3
"""
Public shop prices for Macedonia, from the chains' own price lists.

Since 18 April 2025 shops must publish their prices on their websites every
day (by 10:00), per shop. There is no common format and no barcode, so each
chain is read from its own page and a product is identified by chain + name.
The reference shop of a chain stands for the chain (prices are, in the
main, the same in all its shops). Only the standard library.

  Ramstore   one HTML table per shop (a single page)
  Vero       an HTML table per shop, 500 products to a page (<shop>_<page>.html)

Kipper, Stokomak, Kam and Tinex load their lists with scripts or block plain
requests; they come when someone finds out how to read them politely.

Every product of a chain is kept (one row per product and chain), because the
products can only be matched by name; an offer is a regular price above the
price now. A chain that doesn't answer, or gives far fewer products than usual,
is left out for the day and the others are still written.

  python zbiralnik_mk.py --dry-run          # read, print the numbers
  python zbiralnik_mk.py                    # and write to Supabase
"""
import argparse
import datetime
import hashlib
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request

from skupno import decimal, pisi, pocisti

DRZAVA = "MK"
UA = "Mozilla/5.0 (compatible; SplitidoPriceBot/1.0; +https://github.com/draganQA/splitido-price)"
RAMSTORE = "https://ramstore.com.mk/marketi/ramstore-siti-mol/"
VERO = "https://pricelist.vero.com.mk/{shop}_{page}.html"
VERO_SHOP = "91"   # Vero 2, Karpos


def prenesi(url, poskusov=3):
    for poskus in range(poskusov):
        try:
            zahteva = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "mk,en;q=0.5"})
            with urllib.request.urlopen(zahteva, timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if poskus == poskusov - 1:
                raise
        except Exception:
            if poskus == poskusov - 1:
                raise
        time.sleep(3 * (poskus + 1))


def vrstice_tabele(s):
    """The rows of every table in a page as lists of cell texts."""
    izhod = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", s, re.S | re.I):
        celice = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        if celice:
            izhod.append([" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in celice])
    return izhod


def id_izdelka(veriga, ime):
    return f"{veriga.lower()}:{hashlib.sha1(ime.lower().encode()).hexdigest()[:12]}"


def izdelek(veriga, ime, cena, redna, kategorija):
    """One product of a chain; on offer when the regular price is above the price now."""
    ime = " ".join((ime or "").split())[:200]
    if not ime or not cena or cena <= 0:
        return None
    akcija = bool(redna and redna > cena)
    return {
        "ime": ime, "veriga": veriga, "cena": cena, "akcija": akcija,
        "referencna": redna if akcija else None, "kategorija": (kategorija or "")[:80] or None,
    }


def preberi_ramstore():
    s = prenesi(RAMSTORE)
    izhod = []
    for c in vrstice_tabele(s or ""):
        # name, price now, unit price, category, available, regular price, price with discount, points, type, duration
        if len(c) < 10 or c[4].upper() not in ("ДА", "DA"):
            continue
        i = izdelek("Ramstore", c[0], decimal(c[1]), decimal(c[5]), c[3])
        if i:
            izhod.append(i)
    return izhod


def preberi_vero():
    izhod = []
    for stran in range(1, 400):
        s = prenesi(VERO.format(shop=VERO_SHOP, page=stran))
        if s is None:
            break
        for c in vrstice_tabele(s):
            # name, price now, unit price, available, category, regular price, price with discount, discount %, type, duration
            if len(c) < 10 or c[3].lower() not in ("да", "da"):
                continue
            i = izdelek("Vero", c[0], decimal(c[1]), decimal(c[5]), c[4])
            if i:
                izhod.append(i)
        time.sleep(1)
    return izhod


VERIGE = [("Ramstore", preberi_ramstore, 8000), ("Vero", preberi_vero, 5000)]


def zberi():
    izdelki, cene, stevilo, napake = {}, {}, {}, []
    for ime, bralnik, najmanj in VERIGE:
        try:
            seznam = bralnik()
        except Exception as e:  # a chain that doesn't answer must not stop the others
            napake.append(f"{ime}: {e}")
            continue
        if len(seznam) < najmanj:
            napake.append(f"{ime}: only {len(seznam)} products (at least {najmanj} expected): its page has probably changed")
            continue
        stevilo[ime] = len(seznam)
        for p in seznam:
            ean = id_izdelka(ime, p["ime"])
            prejsnja = cene.get((ean, ime))
            if prejsnja is None or p["cena"] < prejsnja["cena"]:
                cene[(ean, ime)] = {
                    "drzava": DRZAVA, "ean": ean, "veriga": ime, "cena": p["cena"], "akcija": p["akcija"],
                    "referencna": p["referencna"], "najnizja30": None, "sidrena": None,
                }
            if ean not in izdelki:
                izdelki[ean] = {"drzava": DRZAVA, "ean": ean, "ime": p["ime"], "znamka": None, "kolicina": None, "kategorija": p["kategorija"]}
    return izdelki, list(cene.values()), stevilo, napake


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    izdelki, cene, stevilo, napake = zberi()
    for v, n in stevilo.items():
        print(f"  {v}: {n} products")
    for n in napake:
        print("  !", n)
    print(f"Catalogue: {len(izdelki)} products, prices: {len(cene)}", flush=True)
    if not cene:
        sys.exit("Nothing read.")
    if args.dry_run:
        return

    datum = datetime.date.today().isoformat()
    for c in cene:
        c["posodobljeno"] = datum
    url = os.environ["SUPABASE_URL"].rstrip("/")
    kljuc = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    pisi(url, kljuc, "javni_izdelki", list(izdelki.values()), "drzava,ean")
    pisi(url, kljuc, "javne_cene", cene, "drzava,ean,veriga")
    pocisti(url, kljuc, datum, DRZAVA)
    print("Done.")
    if napake:
        sys.exit("Some chains failed: " + "; ".join(napake))


if __name__ == "__main__":
    main()
