"""Mercator's catalogue (raw browse-API answer, a JSON list) -> the file uvozi_datoteko.py imports.
Usage: python pripravi_mercator.py raw.json data/mercator.json.gz"""
import datetime, gzip, json, sys

from zbiralnik_si import id_izdelka, lepo_ime, veljaven_ean

DRZAVA = "SI"


def main(vhod, izhod):
    surovo = json.load(open(vhod, encoding="utf-8"))
    danes = datetime.date.today().isoformat()
    izdelki, cene = {}, []
    for x in surovo:
        try:
            sega, redna = float(x["current_price"]), float(x.get("normal_price") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        ime = lepo_ime(x.get("name"))
        if sega <= 0 or not ime:
            continue
        skupine = {d.get("group_type") for d in x.get("discounts") or []}
        kartica = None
        if "pika_discount" in skupine and redna > 0:   # card-holder price: the shelf price is the regular one
            kartica, sega, redna = sega, redna, 0
        akcija = redna > sega
        ean = next((e for e in (veljaven_ean(g.get("gtin")) for g in x.get("gtins") or []) if e), None) \
            or id_izdelka("mercator", x.get("cinv") or x.get("code"))
        if ean in izdelki:
            continue
        izdelki[ean] = {"drzava": DRZAVA, "ean": ean, "ime": ime,
                        "znamka": lepo_ime(x["brand_name"]) if x.get("brand_name") else None,
                        "kolicina": None, "kategorija": None}
        cene.append({"drzava": DRZAVA, "ean": ean, "veriga": "Mercator", "cena": round(sega, 2), "akcija": akcija,
                     "referencna": round(redna, 2) if akcija else None, "najnizja30": None, "sidrena": None,
                     "cena_kartica": round(kartica, 2) if kartica and kartica < sega else None,
                     "posodobljeno": danes})
    with gzip.open(izhod, "wt", encoding="utf-8") as f:
        json.dump({"datum": danes, "veriga": "Mercator", "izdelki": list(izdelki.values()), "cene": cene}, f,
                  ensure_ascii=False, separators=(",", ":"))
    print(len(izdelki), "products,", sum(c["akcija"] for c in cene), "on sale ->", izhod)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
