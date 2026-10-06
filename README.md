# Public shop prices for Splitido

Shops in some countries must publish their price lists every day. This collects
them into one small table in Splitido's Supabase: for each product, its price in
each chain (and whether the chain has it on offer), so the app can show
"today in Croatia: Metro 1,10 € · Lidl 1,19 € · Konzum 1,25 €".

Meant to live in its **own public repository** (public repositories get free
GitHub Actions minutes). Copy this folder's contents there:

    zbiralnik.py
    .github/workflows/dnevno.yml
    README.md

then add two repository secrets (Settings → Secrets and variables → Actions):

- `SUPABASE_URL` (like `https://xxxx.supabase.co`)
- `SUPABASE_SERVICE_ROLE_KEY` (Project settings → API → `service_role`; never commit it)

The tables and functions are created by Splitido's migration
`20260101000089_javne_cene.sql`; run it first.

## Croatia

Since 15 May 2025 every chain publishes its price lists daily in a machine-readable
form (Odluka NN 75/2025). [cijene.dev](https://cijene.dev) gathers them into one ZIP a
day (free, no registration: https://api.cijene.dev/v0/list), and `zbiralnik.py` boils
that down:

- the price: the most common one across the chain's stores;
- an offer: the chain's `special_price` is set (the price is then already the lowered one);
- the price before the offer: the lowest price of the previous 30 days, else the anchor
  price of 2 May 2025;
- only products sold by two or more chains (so there is something to compare) or on offer.

It refuses to write if far fewer products than usual come out: a chain has probably
changed its format.

    python zbiralnik.py --dry-run              # download, boil down, print the numbers
    python zbiralnik.py --archive file.zip --dry-run --out rows/

If cijene.dev ever goes away: it is open source (github.com/senko/cijene-api, AGPL),
and its crawlers read the chains' own price lists, which the law keeps public.

## More countries

Serbia (`zbiralnik_rs.py`, weekly CSV files on the open data portal), Macedonia (`zbiralnik_mk.py`) and others get their own
`zbiralnik_<country>.py` and a job in the workflow. They write to the same tables with
their own `drzava`.
