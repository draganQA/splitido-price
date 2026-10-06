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

  Stokomak   an HTML table per shop on its price portal, 100 products to a page
  Kam        a PDF per shop (listed by a JSON call on kam.com.mk), read with pdftotext

  Kipper     the page's own table is filled by a call to its admin-ajax.php (500 products at a time)

Tinex publishes its list on ceni.tinex.mk, which doesn't answer from everywhere (or
at all); it comes when it does.

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
import json
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from skupno import decimal, pisi, pocisti

DRZAVA = "MK"
UA = "Mozilla/5.0 (compatible; SplitidoPriceBot/1.0; +https://github.com/draganQA/splitido-price)"
RAMSTORE = "https://ramstore.com.mk/marketi/ramstore-siti-mol/"
VERO = "https://pricelist.vero.com.mk/{shop}_{page}.html"
VERO_SHOP = "91"   # Vero 2, Karpos
STOKOMAK = "https://stokomak.proverkanaceni.mk/index.php?page={page}&perPage=100&search=&org={shop}"
STOKOMAK_SHOP = "1"   # Kisela Voda
KAM = "https://kam.com.mk"
KIPPER = "https://kipper.mk/wp-admin/admin-ajax.php"
KIPPER_SHOP = "8211"   # Kipper 041, Kumanovo: the id of the shop's page


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


def cena(s):
    """A price with its unit as printed ("71 ден.", "1.299ден.") -> float, or None."""
    m = re.search(r"\d[\d.,]*", s or "")
    return decimal(m.group()) if m else None


def preberi_stokomak():
    izhod = []
    for stran in range(1, 200):
        s = prenesi(STOKOMAK.format(page=stran, shop=STOKOMAK_SHOP))
        vrstice = [c for c in vrstice_tabele(s or "") if len(c) >= 6]
        if not vrstice:
            break
        for c in vrstice:
            # name, price now, unit price, description, available, regular price, price with discount, type, duration
            if c[4].lower() not in ("да", "da"):
                continue
            i = izdelek("Stokomak", c[0], cena(c[1]), cena(c[5]), c[3])
            if i:
                izhod.append(i)
        time.sleep(1)
    return izhod


def preberi_kam_pdf(besedilo):
    """The products of a Kam price list as text (pdftotext -layout): one block of lines per product,
    the name in the left column over several lines, the prices on the block's first line."""
    # the heading of every page ("Датум и време на последно ажурирање...") sits right above the first
    # product of the page and must not become part of its name
    vrstice = ["" if "Датум и време на последно" in v else v for v in besedilo.split("\n")]
    stolpec = next((v.index("Продажна") for v in vrstice if "Продажна" in v), None)
    if not stolpec:
        return []
    izhod = []
    blok = []
    for v in vrstice + [""]:
        if v.strip():
            blok.append(v)
            continue
        if blok:
            glava = next((b for b in blok if re.match(r"\s*\d[\d.,]*\s*ден\.", b[stolpec - 2:stolpec + 16])), None)
            if glava:
                ime = " ".join(" ".join(b[:stolpec - 2].split()) for b in blok if b[:stolpec - 2].strip())
                cene = re.findall(r"(\d[\d.,]*)\s*ден\.", glava[stolpec - 2:])
                dostopno = re.search(r"\s(Да|Не)\s", glava[stolpec - 2:])
                if cene and (not dostopno or dostopno.group(1) == "Да"):
                    i = izdelek("Kam", ime, decimal(cene[0]), decimal(cene[1]) if len(cene) > 1 else None, None)
                    if i:
                        izhod.append(i)
        blok = []
    return izhod


def preberi_kam():
    zahteva = urllib.request.Request(f"{KAM}/ShopsWeb/LoadShopList", data=b"{}", method="POST",
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(zahteva, timeout=120) as r:
        trgovine = json.loads(r.read().decode("utf-8"))
    pot = None
    for t in sorted(trgovine, key=lambda t: t.get("Id") or 0):
        if t.get("ShopFiles"):
            pot = max(t["ShopFiles"], key=lambda f: f.get("Id") or 0)["RelativePath"]
            break
    if not pot:
        raise RuntimeError("no price list file in the list of shops")
    with tempfile.TemporaryDirectory() as mapa:
        datoteka = f"{mapa}/kam.pdf"
        zahteva = urllib.request.Request(f"{KAM}/{pot}", headers={"User-Agent": UA})
        with urllib.request.urlopen(zahteva, timeout=180) as r, open(datoteka, "wb") as f:
            f.write(r.read())
        besedilo = subprocess.run(["pdftotext", "-layout", datoteka, "-"], check=True, capture_output=True).stdout.decode("utf-8", "replace")
    return preberi_kam_pdf(besedilo)



def preberi_kipper():
    izhod = []
    start = 0
    while True:
        telo = urllib.parse.urlencode({"action": "get_products_data", "post_id": KIPPER_SHOP, "draw": 1, "start": start, "length": 500}).encode()
        zahteva = urllib.request.Request(KIPPER, data=telo, headers={"User-Agent": UA})
        with urllib.request.urlopen(zahteva, timeout=120) as r:
            odgovor = json.loads(r.read().decode("utf-8"))
        vrstice = odgovor.get("data") or []
        for p in vrstice:
            if p.get("product_status") != "D":
                continue
            cena_zdaj = decimal(str(p.get("product_price") or ""))
            redna = decimal(str(p.get("product_price_normal") or ""))
            i = izdelek("Kipper", p.get("product_name"), cena_zdaj, redna, p.get("product_subgroup"))
            if i:
                izhod.append(i)
        start += 500
        if len(vrstice) < 500 or start >= int(odgovor.get("recordsTotal") or 0):
            break
        time.sleep(1)
    return izhod


VERIGE = [("Ramstore", preberi_ramstore, 8000), ("Vero", preberi_vero, 5000),
          ("Stokomak", preberi_stokomak, 2000), ("Kam", preberi_kam, 800),
          ("Kipper", preberi_kipper, 1500)]


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
