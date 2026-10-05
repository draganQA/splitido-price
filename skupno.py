"""Shared by the collectors: writing the rows to Supabase and cleaning up old prices."""
import datetime
import json
import urllib.parse
import urllib.request

PACKET = 1000


def decimal(s):
    """A price as printed ("1.299,00", "145.00", "45") -> float, or None."""
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "")
    s = s.replace(",", ".")
    try:
        v = round(float(s), 2)
    except ValueError:
        return None
    return v if 0 <= v < 10_000_000 else None


def pisi(url, kljuc, tabela, vrstice, konflikt):
    pot = f"{url}/rest/v1/{tabela}?on_conflict={konflikt}"
    for i in range(0, len(vrstice), PACKET):
        telo = json.dumps(vrstice[i:i + PACKET]).encode()
        zahteva = urllib.request.Request(pot, data=telo, method="POST", headers={
            "apikey": kljuc, "Authorization": f"Bearer {kljuc}", "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })
        urllib.request.urlopen(zahteva, timeout=120).read()
        if (i // PACKET) % 20 == 0:
            print(f"  {tabela}: {min(i + PACKET, len(vrstice))}/{len(vrstice)}", flush=True)


def pocisti(url, kljuc, datum, drzava):
    meja = (datetime.date.fromisoformat(datum) - datetime.timedelta(days=3)).isoformat()
    q = urllib.parse.urlencode({"drzava": f"eq.{drzava}", "posodobljeno": f"lt.{meja}"})
    zahteva = urllib.request.Request(f"{url}/rest/v1/javne_cene?{q}", method="DELETE", headers={
        "apikey": kljuc, "Authorization": f"Bearer {kljuc}", "Prefer": "return=minimal"})
    urllib.request.urlopen(zahteva, timeout=120).read()
