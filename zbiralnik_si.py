#!/usr/bin/env python3
"""
Shop prices for Slovenia, read from the chains' own web pages (Slovenia has no daily open price list
like Croatia or Serbia). Three chains, each read the way its site publishes its products:

  hofer  every product page in www.hofer.si's product sitemap carries schema.org data (name, brand,
         pack size, price in EUR);
  lidl   the same from www.lidl.si's product sitemap; only products that are on sale right now count
         (a page that says OutOfStock or has no price is skipped). Many of Lidl's prices are weekly
         offers, so they come and go;
  spar   online.spar.si: every category page embeds its first products (name, price, previous price)
         in the page; the sitemap lists the categories, and only the lowest level is read.

Polite on purpose: the sites' robots.txt files are followed (no search pages, no paths they disallow),
one request at a time with a pause between them, an honest User-Agent, and no attempt to get around
anything. A chain whose pages changed (far fewer products than usual) is left out for the day.

There are no barcodes in these pages (Hofer, Lidl) or none we can match across chains, so, as in
Macedonia and Bulgaria, a product is identified by chain + the chain's own code and matched to the flat's
products by name. Only the standard library.

  python zbiralnik_si.py --chain hofer --limit 20 --dry-run     # try it on a few pages
  python zbiralnik_si.py --chain all --dry-run                   # all three, nothing written
  python zbiralnik_si.py --chain spar                            # read one chain and write to Supabase
"""
import argparse
import datetime
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from skupno import decimal, pisi, pocisti

DRZAVA = "SI"
UA = "Mozilla/5.0 (compatible; SplitidoPriceBot/1.0; +https://github.com/draganQA/splitido-price)"
PREMOR = {"hofer": 1.2, "lidl": 1.2, "spar": 2.5}      # seconds between two requests to the same site
NAJMANJ = {"hofer": 250, "lidl": 150, "spar": 1000}    # fewer products than this: the pages changed, write nothing


KOLICINA = re.compile(r"^(\d+\s*x\s*)?\d+([.,]\d+)?\s*(g|kg|dag|ml|cl|dl|l|kos|kosov|m|cm)\b", re.I)


def prenesi(url, poskusi=3):
    """The page's text, or None when it does not exist. Waits and tries again on a busy server."""
    for poskus in range(poskusi):
        zahteva = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sl"})
        try:
            with urllib.request.urlopen(zahteva, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308, 404, 410):   # gone, or a redirect that loops (a broken link in a sitemap)
                return None
            if e.code not in (429, 500, 502, 503, 504) or poskus == poskusi - 1:
                raise
            time.sleep(20 * (poskus + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if poskus == poskusi - 1:
                raise
            time.sleep(10 * (poskus + 1))
    return None


def id_izdelka(veriga, koda):
    return f"{veriga}:{hashlib.sha1(str(koda).encode()).hexdigest()[:12]}"


def lepo_ime(s):
    """ "BIO KROMPIR, SPAR NATUR*PUR, 1KG" -> "Bio krompir, spar natur*pur, 1kg": shops print some names in capitals."""
    s = " ".join(html.unescape(s or "").split())
    if s and any(c.isalpha() for c in s) and s == s.upper():
        s = s[:1] + s[1:].lower()
    return s[:200]


def najdi_url(besedilo):
    return [u.strip() for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", besedilo)]


# ---- the Nuxt payload some sites embed (Lidl): a flat JSON array whose containers hold indices into it -----------

NUXT_OZNAKE = {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}


def nuxt_podatki(stran):
    """The page's embedded Nuxt payload as plain nested data, or None."""
    for m in re.finditer(r"<script[^>]*>(\[\[.*?)</script>", stran, re.S):
        try:
            arr = json.loads(m.group(1))
        except ValueError:
            continue

        def razpri(i, globina=0):
            if not isinstance(i, int) or isinstance(i, bool):
                return i
            if i == -1 or globina > 40 or i >= len(arr):
                return None
            v = arr[i]
            if isinstance(v, list):
                if v and isinstance(v[0], str):
                    return razpri(v[1], globina + 1) if v[0] in NUXT_OZNAKE and len(v) > 1 else v
                return [razpri(x, globina + 1) for x in v]
            if isinstance(v, dict):
                return {k: razpri(x, globina + 1) for k, x in v.items()}
            return v

        try:
            return razpri(0)
        except RecursionError:
            return None
    return None


def veljaven_ean(v):
    v = str(v or "").strip()
    return v if v.isdigit() and len(v) in (8, 12, 13, 14) else None


def lidl_podrobnosti(stran):
    """(barcode, pack size text) of a Lidl product page, from its embedded payload; (None, None) when absent."""
    koren = nuxt_podatki(stran)
    podatki = (koren or {}).get("data") if isinstance(koren, dict) else None
    if not isinstance(podatki, dict):
        return None, None
    for izdelek in podatki.values():
        if isinstance(izdelek, dict) and "eans" in izdelek:
            eani = izdelek.get("eans") or []
            cena = izdelek.get("price") if isinstance(izdelek.get("price"), dict) else {}
            pakiranje = (cena.get("packaging") or {}).get("text") if isinstance(cena.get("packaging"), dict) else None
            return veljaven_ean(eani[0] if eani else None), (str(pakiranje).strip() if pakiranje else None)
    return None, None


# ---- schema.org product pages (Hofer, Lidl) ---------------------------------------------------------

def izdelek_ld(stran):
    """The schema.org Product object of a page, or None."""
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', stran, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for kos in d if isinstance(d, list) else [d]:
            if isinstance(kos, dict) and kos.get("@type") == "Product":
                return kos
    return None


def ponudba(d):
    o = d.get("offers")
    if isinstance(o, list):
        o = o[0] if o else None
    return o if isinstance(o, dict) else None


def preberi_ld(stran, veriga):
    """One product page -> (code, name, brand, quantity, price) or None when the page has no usable price."""
    d = izdelek_ld(stran)
    if not d:
        return None
    o = ponudba(d)
    if not o or "OutOfStock" in str(o.get("availability", "")) or o.get("priceCurrency", "EUR") != "EUR":
        return None
    cena = decimal(str(o.get("price", "")))
    ime = lepo_ime(d.get("name"))
    if not cena or cena <= 0 or not ime:
        return None
    znamka = d.get("brand")
    znamka = znamka.get("name") if isinstance(znamka, dict) else znamka
    koda = d.get("sku") or d.get("productID") or (o.get("url") or d.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    opis = lepo_ime(d.get("description"))
    kolicina = opis if KOLICINA.match(opis) else None
    ean = None
    if veriga == "lidl":
        ean, pakiranje = lidl_podrobnosti(stran)
        kolicina = kolicina or pakiranje
    return str(koda), ime, (lepo_ime(znamka) if znamka else None), kolicina, cena, ean


def zberi_ld(veriga, naslovi, omejitev):
    """Every product page of a chain -> [(code, name, brand, quantity, price, offer, regular price)]."""
    if omejitev:
        naslovi = naslovi[:omejitev]
    print(f"  {veriga}: {len(naslovi)} product pages", flush=True)
    izhod, brez, zaporedne_napake = {}, 0, 0
    for i, url in enumerate(naslovi):
        try:
            stran = prenesi(url)
            zaporedne_napake = 0
        except Exception as e:  # noqa: BLE001
            zaporedne_napake += 1
            print(f"  ! {url}: {e}")
            if zaporedne_napake >= 8:
                raise RuntimeError(f"{veriga}: the site stopped answering")
            time.sleep(PREMOR[veriga])
            continue
        p = preberi_ld(stran, veriga) if stran else None
        if p:
            izhod[p[0]] = p
        else:
            brez += 1
        if (i + 1) % 100 == 0:
            print(f"  {veriga}: {i + 1}/{len(naslovi)}, with a price: {len(izhod)}", flush=True)
        time.sleep(PREMOR[veriga])
    print(f"  {veriga}: {len(izhod)} with a price, {brez} without")
    return [(k, ime, znamka, kol, cena, False, None, ean) for k, (_, ime, znamka, kol, cena, ean) in izhod.items()]


def zberi_hofer(omejitev):
    vsebina = prenesi("https://www.hofer.si/sitemap_products.xml")
    if not vsebina:
        raise RuntimeError("hofer: no sitemap")
    return zberi_ld("hofer", [u for u in najdi_url(vsebina) if "/izdelek/" in u], omejitev)


def zberi_lidl(omejitev):
    """Lidl's product sitemap is a gzipped file."""
    import gzip
    zahteva = urllib.request.Request("https://www.lidl.si/p/export/SI/sl/product_sitemap.xml.gz", headers={"User-Agent": UA})
    with urllib.request.urlopen(zahteva, timeout=60) as r:
        surovo = r.read()
    try:
        vsebina = gzip.decompress(surovo).decode("utf-8", "replace")
    except OSError:
        vsebina = surovo.decode("utf-8", "replace")
    return zberi_ld("lidl", [u for u in najdi_url(vsebina) if "/p/" in u], omejitev)


# ---- Spar: the products embedded in a category page ---------------------------------------------------

def izdelki_spar(stran):
    """The products embedded in a category page; each has `ean` resolved to its barcode (or None)."""
    kosi = re.findall(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', stran, re.S)
    tok = "".join(json.loads(k) for k in kosi)
    izdelki, reference = [], {}
    for vrstica in tok.split("\n"):
        m = re.match(r"^([0-9a-f]+):(\[.*|\{.*)$", vrstica)
        if not m:
            continue
        try:
            v = json.loads(m.group(2))
        except ValueError:
            continue
        reference[m.group(1)] = v
        if isinstance(v, dict) and v.get("__typename") == "CatalogProductModel":
            izdelki.append(v)
    for p in izdelki:
        e = p.get("ean")
        e = reference.get(e[1:]) if isinstance(e, str) and e.startswith("$") else e
        p["ean"] = veljaven_ean(e[0] if isinstance(e, list) and e else e)
    return izdelki


def zberi_spar(omejitev):
    vsebina = prenesi("https://online.spar.si/sitemap-categories.xml")
    if not vsebina:
        raise RuntimeError("spar: no sitemap")
    # the lowest level (/S1/S1-1/S1-1-4): its pages hold the products without the ones of other categories
    kategorije = [u for u in najdi_url(vsebina) if re.search(r"/S\d+/S\d+-\d+/S\d+-\d+-\d+$", u)]
    if omejitev:
        kategorije = kategorije[:omejitev]
    print(f"  spar: {len(kategorije)} categories", flush=True)
    izhod, zaporedne_napake = {}, 0
    for i, url in enumerate(kategorije):
        try:
            stran = prenesi(url)
            zaporedne_napake = 0
        except Exception as e:  # noqa: BLE001
            zaporedne_napake += 1
            print(f"  ! {url}: {e}")
            if zaporedne_napake >= 8:
                raise RuntimeError("spar: the site stopped answering")
            time.sleep(PREMOR["spar"])
            continue
        for p in izdelki_spar(stran or ""):
            cena, ime = decimal(str(p.get("price", ""))), lepo_ime(p.get("name"))
            if not cena or cena <= 0 or not ime or not p.get("sku") or p.get("isAvailable") is False:
                continue
            prej = decimal(str(p.get("previousPrice") or ""))
            akcija = bool(prej and prej > cena)
            znamka = lepo_ime(p["brand"]) if p.get("brand") else None
            izhod[str(p["sku"])] = (str(p["sku"]), ime, znamka, None, cena, akcija, prej if akcija else None, p.get("ean"))
        if (i + 1) % 50 == 0:
            print(f"  spar: {i + 1}/{len(kategorije)}, products: {len(izhod)}", flush=True)
        time.sleep(PREMOR["spar"])
    print(f"  spar: {len(izhod)} products")
    return list(izhod.values())


VERIGE = {"hofer": ("Hofer", zberi_hofer), "lidl": ("Lidl", zberi_lidl), "spar": ("Spar", zberi_spar)}


def v_vrstice(ime_verige, seznam):
    """Rows for the tables. A product with a barcode is one catalogue row for every chain that sells it (the barcode is
    its id); one without is the chain's own code."""
    izdelki, cene = {}, []
    for koda, ime, znamka, kolicina, cena, akcija, redna, ean_izdelka in seznam:
        ean = ean_izdelka or id_izdelka(ime_verige.lower(), koda)
        izdelki[ean] = {"drzava": DRZAVA, "ean": ean, "ime": ime, "znamka": znamka, "kolicina": kolicina, "kategorija": None}
        cene.append({"drzava": DRZAVA, "ean": ean, "veriga": ime_verige, "cena": cena, "akcija": bool(akcija),
                     "referencna": redna, "najnizja30": None, "sidrena": None})
    return izdelki, cene


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", choices=["hofer", "lidl", "spar", "all"], default="all")
    ap.add_argument("--limit", type=int, help="read only this many pages per chain (for a test)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="write the rows as JSON to this file (for tests)")
    args = ap.parse_args()

    izbrane = list(VERIGE) if args.chain == "all" else [args.chain]
    izdelki, cene, stevilo, napake = {}, [], {}, []
    for ime in izbrane:
        veriga, bralnik = VERIGE[ime]
        print(f"{veriga}:", flush=True)
        try:
            seznam = bralnik(args.limit)
        except Exception as e:  # noqa: BLE001
            napake.append(f"{veriga}: {e}")
            continue
        if not args.limit and len(seznam) < NAJMANJ[ime]:
            napake.append(f"{veriga}: only {len(seznam)} products (at least {NAJMANJ[ime]} expected): the pages have probably changed")
            continue
        i, c = v_vrstice(veriga, seznam)
        izdelki.update(i)
        cene.extend(c)
        stevilo[veriga] = len(seznam)
    for v, n in stevilo.items():
        print(f"  {v}: {n} products")
    for n in napake:
        print("  !", n)
    print(f"Catalogue: {len(izdelki)} products, prices: {len(cene)}", flush=True)
    if not cene:
        sys.exit("Nothing read." + (" " + "; ".join(napake) if napake else ""))

    datum = datetime.date.today().isoformat()
    for c in cene:
        c["posodobljeno"] = datum
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"izdelki": list(izdelki.values()), "cene": cene}, f, ensure_ascii=False)
    if args.dry_run or args.limit:
        return

    url = os.environ["SUPABASE_URL"].rstrip("/")
    kljuc = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    pisi(url, kljuc, "javni_izdelki", list(izdelki.values()), "drzava,ean")
    pisi(url, kljuc, "javne_cene", cene, "drzava,ean,veriga")
    pocisti(url, kljuc, datum, DRZAVA)
    print("Done.")
    if napake:
        sys.exit("Failed: " + "; ".join(napake))


if __name__ == "__main__":
    main()
