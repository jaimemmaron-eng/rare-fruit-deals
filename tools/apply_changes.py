#!/usr/bin/env python3
"""Rare Fruit Deals: apply one night's research to a fruit's data file, safely.

The nightly task never edits data/<fruit>.json by hand. It writes a small changes file (JSON,
format below) and this script applies it, refusing anything malformed and holding back anything
suspicious for review instead of publishing it.

    python3 apply_changes.py brief  data.json                 # compact view of one fruit's data to work from
    python3 apply_changes.py apply  data.json changes.json out.json
    python3 apply_changes.py vendors DATA_DIR [--batch A|B]   # every nursery website across ALL fruits, one block per site
    python3 apply_changes.py apply-batch DATA_DIR batch.json OUT_DIR   # apply one run's changes to several fruits at once
    python3 apply_changes.py alerts DATA_DIR watch.json                # tonight's alerts for the owner's watched varieties
    python3 apply_changes.py rotation today [fruit_rules.json]          # tonight's fruit (New York date) for the seller search, with its rules

The nightly price checks go nursery by nursery, not fruit by fruit: `vendors` groups every fruit's listings
by website, so each site is visited once a night and all of its fruits are updated from that visit. Sites
are split into two fixed halves (A and B, by a hash of the domain) for the two nightly batches.
`apply-batch` takes {"date": "...", "fruits": {"pawpaw": {<changes for pawpaw.json>}, "kiwi": {...}}}
(each fruit's block uses the per-fruit changes format below, without "date") and writes OUT_DIR/<fruit>.json
for every fruit that passes validation. Several runs on the same night merge their last_run and history lines.

`apply` exits 0 and writes out.json only if the result passes every check; it prints a report
(applied / held / rejected). Held items are recorded in last_run.held so they show up for review.

changes.json:
{
  "date": "YYYY-MM-DD",                      # today, America/New_York
  "checked_nurseries": ["id", ...],          # nurseries whose pages were actually read tonight
  "failed_nurseries": [{"id": "x", "reason": "timeout"}],
  "listing_updates": [{"id": "<nursery>--<variety>", "status": "...", "price": 0.0,
                       "price_from": bool, "price_confirmed": bool, "url": "...", "ships": "...",
                       "sold_as": "...", "was_price": 0.0, "on_sale": bool, "notes": ["..."]}],      # only fields that changed
  "new_nurseries": [{full nursery object}],
  "nursery_updates": [{"id": "x", "shipping": {...}, "note": "...", "url": "..."}],
  "new_varieties": [{full variety object}],
  "variety_updates": [{"id": "x", "add_aliases": ["..."], "summary": "...", "description": "...",
                       "breeder": {...}, "add_awards": ["..."], "source": "https://...",
                       "bloom": "...", "color": "...", ...}],   # fruit-specific fields allowed too
  "new_listings": [{"variety": "v", "nursery": "n", "status": "...", "price": 0.0, "url": "...", ...}],
  "sources": {"add_checked_no_named_stock": [], "remove_checked_no_named_stock": [], "add_dead": [],
              "add_to_check": [], "remove_to_check": []},
  "summary": "one or two sentences", "gaps": ["..."], "history_line": "Oct 5: ..."
}
The script writes each night's story itself from what changed (restocks, sell-outs, price moves, new listings,
naming the variety and nursery), so summary/history_line only need what the data can't show: new sellers found,
why a site was skipped, anything odd. Don't restate counts. Dates are New York dates; a UTC date is corrected.

Checking tips (learned from earlier runs):
- When a category page and the product page disagree about stock, trust the product page (an "Add to cart"
  button, or og:availability "instock" for that product) and say so in the listing's notes. WooCommerce
  category pages (One Green World and others) often show "Email me when available" for products that are
  buyable in some size.
- Before reporting a whole lineup as sold out, open at least two of its product pages: it may be real
  (end of season) or a misread category page. The script holds mass sell-outs for review either way;
  mention them in your final message rather than working around the hold.
- Before calling a site unreadable, try its listings' own product pages (the url on each listing) and the store's
  JSON: Shopify /products.json or <product url>.js, WooCommerce /wp-json/wc/store/v1/products?search=<variety>, or a
  product page's JSON-LD (offers.price / availability). Burnt Ridge's /mobile/...productinfo/... pages show the size
  and price ("liner $6.50"). Only if none of that shows a per-variety price, list the nursery in failed_nurseries
  with reason "no per-variety prices". After two such nights the board stops ranking its prices as deals.
- Note the size a price is for when it isn't the usual plant (liner, plug, 4-inch pot, cutting) in sold_as.
- Sales: when a store shows a marked-down price, send was_price = its regular price (Shopify "compare_at_price",
  WooCommerce regular_price vs sale_price, a struck-through price, "was $X", "reg. $X"). If it says "sale" but shows
  no regular price, send on_sale: true. When a sale you see on file has ended, send was_price: null / on_sale: false.
  Member-only or coupon prices are not sales; mention them in notes. The vendors list shows "(sale, was X)" for
  listings already marked on sale. Leaving stock clears a sale automatically.
- Report stock as you see it. If a nursery lists states it "can't ship to" (common for gooseberries and
  currants), record them with nursery_updates {"id": "x", "excluded_states": ["NJ", ...]}. The script then
  shows its in-stock listings as no_ship ("Doesn't ship to NJ"), because the board prices delivery to 08801.
"""
import json, sys, copy

STATUSES = {"in_stock", "sold_out", "unconfirmed", "season_closed", "no_ship"}
HOME_STATE = "NJ"   # the board's ship-to (08801); listings from nurseries that exclude it become no_ship
SHIP_TYPES = {"flat", "percent", "table", "included", "pickup", "estimate"}
LISTING_FIELDS = {"status", "price", "price_from", "price_confirmed", "url", "ships", "sold_as", "notes", "ship_est", "was_price", "on_sale"}
NURSERY_FIELDS = {"name", "location", "state", "url", "note", "phone", "distance", "shipping", "excluded_states"}
VARIETY_PROTECTED = {"id", "fruit", "first_seen", "aliases", "awards"}   # everything else (incl. fruit-specific fields) may be updated
MAX_PRICE_SWING = 0.6           # a price moving more than 60% in one night is held for review
PRICE_RANGE = (2, 1500)
MASS_SELLOUT = 0.4              # more than 40% of in-stock listings selling out in one night is held

class Report:
    def __init__(s): s.applied, s.held, s.rejected = [], [], []
    def ok(s, m): s.applied.append(m)
    def hold(s, m): s.held.append(m)
    def bad(s, m): s.rejected.append(m)

def num(x): return isinstance(x, (int, float)) and not isinstance(x, bool)

def check_listing_values(u, r, lid):
    if "status" in u and u["status"] not in STATUSES: r.bad(f"{lid}: bad status {u['status']!r}"); return False
    if "was_price" in u and u["was_price"] is not None and not num(u["was_price"]): r.bad(f"{lid}: was_price must be a number or null"); return False
    if "on_sale" in u and not isinstance(u["on_sale"], bool): r.bad(f"{lid}: on_sale must be true or false"); return False
    if "price" in u and u["price"] is not None:
        if not num(u["price"]): r.bad(f"{lid}: price must be a number or null"); return False
        if not PRICE_RANGE[0] <= u["price"] <= PRICE_RANGE[1]: r.hold(f"{lid}: price {u['price']} outside {PRICE_RANGE}"); return False
    return True

def validate(d, fruit):
    errs = []
    if d.get("fruit") != fruit: errs.append(f"fruit is {d.get('fruit')!r}, expected {fruit!r}")
    nv = [v["id"] for v in d["varieties"]]; nn = [n["id"] for n in d["nurseries"]]; nl = [l["id"] for l in d["listings"]]
    for name, xs in (("variety", nv), ("nursery", nn), ("listing", nl)):
        dup = {x for x in xs if xs.count(x) > 1}
        if dup: errs.append(f"duplicate {name} ids {sorted(dup)}")
    sv, sn = set(nv), set(nn)
    for l in d["listings"]:
        if l["variety"] not in sv: errs.append(f"{l['id']}: unknown variety")
        if l["nursery"] not in sn: errs.append(f"{l['id']}: unknown nursery")
        if l["id"] != f"{l['nursery']}--{l['variety']}": errs.append(f"{l['id']}: id must be nursery--variety")
        if l["status"] not in STATUSES: errs.append(f"{l['id']}: bad status")
        if l.get("price") is not None and not num(l["price"]): errs.append(f"{l['id']}: bad price")
    used = {l["nursery"] for l in d["listings"]}
    for n in d["nurseries"]:
        if n["id"] not in used: errs.append(f"nursery {n['id']} has no listings")
        if (n.get("shipping") or {}).get("type") not in SHIP_TYPES: errs.append(f"nursery {n['id']}: bad shipping type")
    for v in d["varieties"]:
        b = v.get("breeder")
        if b and b.get("kind") not in ("university", "breeder"): errs.append(f"{v['id']}: bad breeder kind")
    return errs

# ---------------------------------------------------------------- the night's story, written from the data itself
# The history line and summary are built from what actually changed (listing history entries dated tonight), so
# every fruit gets a specific account even when the checker's own wording is thin. The checker's text is kept
# only when it adds something beyond a count ("found one new seller, Territorial Seed ...").
import re, datetime
GENERIC = re.compile(r"(\bchecked \d+ nurser|\b\d+ listing (update|change)|listing (update|change)\(s\)|batch [ab] checks?\b|^\s*no (other )?changes?\b|^\s*nothing changed|^\s*no stock or price changes)", re.I)
STATUS_WORD = {"sold_out": "sold out", "season_closed": "closed for the season", "unconfirmed": "unconfirmed",
               "no_ship": "won't ship to NJ", "in_stock": "in stock"}

def specific(text):
    """the parts of the checker's text that say more than a count"""
    parts = [p.strip(" .;") for p in re.split(r"(?<=[.;])\s+", text or "")]
    return [p[0].upper() + p[1:] for p in parts if p and not GENERIC.search(p)]

def money(x): return "" if x is None else ("$%d" % x if float(x).is_integer() else "$%.2f" % x)

def day_events(d, date):
    """what changed on `date`, from each listing's history (entries are only added on a change):
    {kind: {nursery name: [item, ...]}} with kind in new/back/out/price/other"""
    N = {n["id"]: n.get("name", n["id"]) for n in d["nurseries"]}; V = {v["id"]: v.get("name", v["id"]) for v in d["varieties"]}
    ev = {k: {} for k in ("back", "out", "price", "new", "other")}
    def add(k, l, item): ev[k].setdefault(N.get(l["nursery"], l["nursery"]), []).append(item)
    for l in d["listings"]:
        h = l.get("history") or []
        idx = [i for i, e in enumerate(h) if e.get("date") == date]
        if not idx: continue
        after = h[idx[-1]]; before = h[idx[0] - 1] if idx[0] > 0 else None
        name = V.get(l["variety"], l["variety"])
        s1, p1 = after.get("status"), after.get("price")
        if before is None:
            add("new", l, f"{name} ({money(p1) if s1 == 'in_stock' and p1 is not None else STATUS_WORD.get(s1, s1)})"); continue
        s0, p0 = before.get("status"), before.get("price")
        if s1 == "in_stock" and s0 != "in_stock": add("back", l, name + (f" {money(p1)}" if p1 is not None else ""))
        elif s0 == "in_stock" and s1 in ("sold_out", "season_closed"): add("out", l, name + (" (season over)" if s1 == "season_closed" else ""))
        elif s0 == s1 and p0 is not None and p1 is not None and p0 != p1: add("price", l, f"{name} {money(p0)} \u2192 {money(p1)}")
        elif s0 != s1: add("other", l, f"{name} now {STATUS_WORD.get(s1, s1)}")
        elif p0 != p1 and p1 is not None: add("price", l, f"{name} priced at {money(p1)}")
    return ev

def story(ev, cap=8):
    def lst(xs): return ", ".join(xs[:cap]) + (f" and {len(xs) - cap} more" if len(xs) > cap else "")
    label = {"back": "Back in stock at {n}: ", "out": "Sold out at {n}: ", "price": "Price changes at {n}: ",
             "new": "New listings at {n}: ", "other": "{n}: "}
    return [label[k].format(n=n) + lst(xs) for k in label for n, xs in ev[k].items()]

def _words(t): return set(re.findall(r"[a-z0-9]+", t.lower()))
def similar(a, b):
    A, B = _words(a), _words(b)
    return bool(A and B) and len(A & B) / min(len(A), len(B)) >= 0.6

def day_tag(date):
    try: x = datetime.date.fromisoformat(date); return f"{x.strftime('%b')} {x.day}"
    except Exception: return date

def finish_run(d, today, c, checked, r):
    gaps = c.get("gaps", []) + [f"{f['id']}: {f.get('reason', 'failed')}" for f in c.get("failed_nurseries", [])]
    prev = d.get("last_run") or {}
    same = prev.get("date") == today    # a second run tonight (the other batch, or the search): merge, don't overwrite
    notes = list(prev.get("notes") or []) if same else []
    for t in specific(c.get("summary", "")) + specific((c.get("history_line") or "").split(":", 1)[-1]):
        if t and not any(similar(t, n) for n in notes): notes.append(t)
    all_checked = (set(prev.get("checked") or []) | checked) if same else checked
    held = list(dict.fromkeys(((prev.get("held") or []) if same else []) + r.held))
    told = story(day_events(d, today))
    head = (". ".join(told) + ".") if told else (f"No stock or price changes at the {len(all_checked)} nurseries checked." if all_checked else "")
    summary = " ".join([head] + [n.rstrip(".") + "." for n in notes]).strip()
    d["last_run"] = dict(date=today, summary=summary, gaps=list(dict.fromkeys(((prev.get("gaps") or []) if same else []) + gaps)),
                         checked=sorted(all_checked), held=held, notes=notes)
    body = "; ".join(told + notes) or (f"no changes at the {len(all_checked)} nurseries checked" if all_checked else "")
    if not body: return
    tag = day_tag(today); hl = d.setdefault("history_log", [])
    if hl and hl[-1].split(":")[0] == tag and same: hl[-1] = f"{tag}: {body}"
    elif hl and hl[-1].split(":")[0] == tag: hl[-1] = hl[-1].rstrip(" .") + "; " + body
    else: hl.append(f"{tag}: {body}")

def et_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).date().isoformat()

def fix_date(date):
    """the board's day is the New York day; a UTC date (a day ahead in the evening) is corrected"""
    et = et_today()
    if not date or date > et:
        if date: print(f"NOTE date {date} is ahead of today in New York ({et}); using {et}")
        return et
    return date

def apply(d, c):
    r = Report(); d = copy.deepcopy(d); today = c["date"]; fruit = d["fruit"]
    N = {n["id"]: n for n in d["nurseries"]}; V = {v["id"]: v for v in d["varieties"]}; L = {l["id"]: l for l in d["listings"]}
    failed = {f["id"] for f in c.get("failed_nurseries", [])}
    checked = set(c.get("checked_nurseries", [])) - failed
    for x in checked - set(N) - {n["id"] for n in c.get("new_nurseries", [])}: r.bad(f"checked_nurseries: unknown nursery {x}")

    for n in c.get("new_nurseries", []):
        if n.get("id") in N: r.bad(f"new nursery {n.get('id')} already exists (use nursery_updates)"); continue
        if not n.get("id") or not n.get("name") or not n.get("url"): r.bad(f"new nursery needs id, name, url: {n}"); continue
        if (n.get("shipping") or {}).get("type") not in SHIP_TYPES: r.bad(f"new nursery {n['id']}: bad shipping"); continue
        n["_new"] = True; d["nurseries"].append(n); N[n["id"]] = n; r.ok(f"+ nursery {n['id']}")
    for u in c.get("nursery_updates", []):
        n = N.get(u.get("id"))
        if not n: r.bad(f"nursery update for unknown {u.get('id')}"); continue
        extra = set(u) - NURSERY_FIELDS - {"id"}
        if extra: r.bad(f"nursery {n['id']}: unknown fields {sorted(extra)}"); continue
        if "shipping" in u and u["shipping"].get("type") not in SHIP_TYPES: r.bad(f"nursery {n['id']}: bad shipping"); continue
        if "shipping" in u and u["shipping"].get("confirmed") and not u["shipping"].get("source"): r.bad(f"nursery {n['id']}: confirmed shipping needs a source url"); continue
        n.update({k: v for k, v in u.items() if k != "id"}); r.ok(f"~ nursery {n['id']}: {', '.join(k for k in u if k != 'id')}")

    for v in c.get("new_varieties", []):
        if v.get("id") in V: r.bad(f"new variety {v.get('id')} already exists"); continue
        if not all(v.get(k) for k in ("id", "name", "summary", "description")): r.bad(f"new variety needs id, name, summary, description: {v.get('id')}"); continue
        if (v.get("breeder") or v.get("awards")) and not v.get("source"): r.hold(f"new variety {v['id']}: breeder/award without a source url (added without them)"); v["breeder"] = None; v["awards"] = []
        v.pop("source", None); v.setdefault("fruit", fruit); v.setdefault("aliases", []); v.setdefault("awards", []); v.setdefault("breeder", None)
        v.setdefault("notes", []); v.setdefault("first_seen", today)
        d["varieties"].append(v); V[v["id"]] = v; r.ok(f"+ variety {v['id']}")
    for u in c.get("variety_updates", []):
        v = V.get(u.get("id"))
        if not v: r.bad(f"variety update for unknown {u.get('id')}"); continue
        extra = (set(u) & VARIETY_PROTECTED) - {"id"}
        if extra: r.bad(f"variety {v['id']}: can't overwrite {sorted(extra)} (use add_aliases / add_awards)"); continue
        if ("breeder" in u or u.get("add_awards")) and not u.get("source"): r.hold(f"variety {v['id']}: breeder/award change without a source url"); continue
        for a in u.get("add_aliases", []):
            if a not in v["aliases"] and a != v["name"]: v["aliases"].append(a)
        for a in u.get("add_awards", []):
            if a not in v["awards"]: v["awards"].append(a)
        for k in set(u) - VARIETY_PROTECTED - {"add_aliases", "add_awards", "source"}: v[k] = u[k]
        r.ok(f"~ variety {v['id']}")

    for l in c.get("new_listings", []):
        lid = f"{l.get('nursery')}--{l.get('variety')}"
        if lid in L: r.bad(f"new listing {lid} already exists (use listing_updates)"); continue
        if l.get("variety") not in V or l.get("nursery") not in N: r.bad(f"new listing {lid}: unknown variety or nursery"); continue
        if not check_listing_values(l, r, lid): continue
        st = l.get("status", "unconfirmed")
        if st == "in_stock" and l.get("price") is None: st = "unconfirmed"; r.ok(f"{lid}: in stock with no price -> unconfirmed")
        new = dict(id=lid, variety=l["variety"], nursery=l["nursery"], status=st, price=l.get("price"),
                   price_from=bool(l.get("price_from")), price_confirmed=l.get("price_confirmed", l.get("price") is not None),
                   url=l.get("url") or N[l["nursery"]]["url"], first_seen=today, last_checked=today, notes=l.get("notes", []),
                   history=[dict(date=today, price=l.get("price"), status=st)])
        for k in ("ships", "sold_as", "ship_est", "was_price", "on_sale"):
            if l.get(k): new[k] = l[k]
        d["listings"].append(new); L[lid] = new; r.ok(f"+ listing {lid} ({st}{', $' + str(new['price']) if new['price'] is not None else ''})")

    ups = c.get("listing_updates", [])
    in_stock_before = sum(1 for l in L.values() if l["status"] == "in_stock")
    sellouts = [u for u in ups if u.get("status") in ("sold_out", "season_closed") and L.get(u.get("id"), {}).get("status") == "in_stock"]
    mass = len(sellouts) > max(5, MASS_SELLOUT * in_stock_before)
    if mass: r.hold(f"{len(sellouts)} of {in_stock_before} in-stock listings would sell out tonight; all sell-outs held for review")
    for u in ups:
        lid = u.get("id"); l = L.get(lid)
        if not l: r.bad(f"update for unknown listing {lid} (use new_listings)"); continue
        extra = set(u) - LISTING_FIELDS - {"id"}
        if extra: r.bad(f"{lid}: unknown fields {sorted(extra)}"); continue
        if l["nursery"] in failed: r.bad(f"{lid}: its nursery failed to load tonight; left unchanged"); continue
        if not check_listing_values(u, r, lid): continue
        if mass and u in sellouts: continue
        p0, p1 = l.get("price"), u.get("price", l.get("price"))
        if num(p0) and num(p1) and p0 > 0 and abs(p1 - p0) / p0 > MAX_PRICE_SWING:
            r.hold(f"{lid}: price {p0} -> {p1} is a {round(abs(p1 - p0) / p0 * 100)}% move"); continue
        before = (l["status"], l.get("price"))
        for k in LISTING_FIELDS & set(u): l[k] = u[k]
        if l["status"] == "in_stock" and l.get("price") is None: l["status"] = "unconfirmed"
        # a sale ends when the item leaves stock, or when the "was" price is no longer above the price
        if l["status"] != "in_stock" or not (num(l.get("was_price")) and num(l.get("price")) and l["was_price"] > l["price"]):
            if l.get("was_price") is not None: l["was_price"] = None
        if l["status"] != "in_stock": l.pop("on_sale", None)
        if "price" in u and u["price"] is not None and "price_confirmed" not in u: l["price_confirmed"] = True
        l["last_checked"] = today
        if (l["status"], l.get("price")) != before:
            l.setdefault("history", []).append(dict(date=today, price=l.get("price"), status=l["status"]))
            r.ok(f"~ {lid}: {before[0]} {before[1]} -> {l['status']} {l.get('price')}")

    # a nursery that won't ship this fruit to the board's home state: in_stock there means no_ship here
    blocked = {n["id"] for n in d["nurseries"] if HOME_STATE in (n.get("excluded_states") or [])}
    for l in d["listings"]:
        if l["nursery"] in blocked and l["status"] == "in_stock":
            l["status"] = "no_ship"; r.ok(f"~ {l['id']}: in stock, but {HOME_STATE} is on the nursery's excluded list -> no_ship")
    for l in d["listings"]:
        if l["nursery"] in checked: l["last_checked"] = today
    used = {l["nursery"] for l in d["listings"]}
    for n in list(d["nurseries"]):
        if n["id"] not in used and n.get("_new"): d["nurseries"].remove(n); r.bad(f"new nursery {n['id']} dropped: no listings (put it in sources.add_checked_no_named_stock instead)")
    for n in d["nurseries"]: n.pop("_new", None)

    s = d.setdefault("sources", {})
    for key, field in (("add_checked_no_named_stock", "checked_no_named_stock"), ("add_dead", "dead"), ("add_to_check", "to_check")):
        for x in c.get("sources", {}).get(key, []):
            if x not in s.setdefault(field, []): s[field].append(x)
    for key, field in (("remove_to_check", "to_check"), ("remove_checked_no_named_stock", "checked_no_named_stock")):
        for x in c.get("sources", {}).get(key, []):
            if x in s.get(field, []): s[field].remove(x)
            else: r.bad(f"sources.{key}: {x!r} not found (copy the exact text from the brief)")

    # a nursery whose page wouldn't load or showed no per-variety prices: count the nights, so the board can stop
    # trusting its old prices after two; one good read clears it
    for f in c.get("failed_nurseries", []):
        n = N.get(f.get("id"))
        if not n: continue
        u = n.get("unreadable") or {}
        n["unreadable"] = dict(since=u.get("since") or today, nights=(u.get("nights") or 0) + (u.get("last") != today), last=today,
                               reason=f.get("reason") or u.get("reason") or "failed")
    for nid in checked:
        if nid in N: N[nid].pop("unreadable", None)
    d["updated"] = today
    finish_run(d, today, c, checked, r)
    return d, r

def brief(d):
    N = {n["id"]: n for n in d["nurseries"]}
    print(f"# {d['fruit']}: {len(d['varieties'])} varieties, {len(d['listings'])} listings, {len(d['nurseries'])} nurseries; last run {d.get('last_run', {}).get('date')}")
    print("\n## nurseries: id | name | shipping | url")
    for n in d["nurseries"]:
        s = n.get("shipping", {}); print(f"{n['id']} | {n['name']} | {s.get('type')}{'*' if s.get('confirmed') else ''} | {n['url']}" + (f" | won't ship to {','.join(n['excluded_states'])}" if n.get("excluded_states") else ""))
    print("\n## varieties: id | name | aliases")
    for v in d["varieties"]: print(f"{v['id']} | {v['name']} | {', '.join(v.get('aliases', []))}")
    print("\n## listings: id | status | price | last_checked | url")
    for l in sorted(d["listings"], key=lambda l: (l["nursery"], l["variety"])):
        print(f"{l['id']} | {l['status']} | {l.get('price')}{'+' if l.get('price_from') else ''} | {l.get('last_checked')} | {l['url']}")
    s = d.get("sources", {})
    print("\n## sources\nchecked_no_named_stock:", "; ".join(map(str, s.get("checked_no_named_stock", []))))
    print("to_check:", "; ".join(map(str, s.get("to_check", []))))
    print("dead:", "; ".join(map(str, s.get("dead", []))))

# ---------------------------------------------------------------- cross-fruit: one block per nursery website
import os, zlib, datetime
from urllib.parse import urlparse
SKIP_FILES = {"fruits.json", "shipping.json", "ship_timing.json", "coupons.json"}
RECHECK_DAYS = 7   # a site with nothing in stock is re-checked about weekly, not nightly

def load_dir(dd):
    if os.path.isdir(os.path.join(dd, "data")) and not any(f.endswith(".json") and f not in SKIP_FILES for f in os.listdir(dd)):
        dd = os.path.join(dd, "data")   # an Artifact read with out_dir saves files under <out_dir>/data/
    out = {}
    for f in sorted(os.listdir(dd)):
        if f.endswith(".json") and f not in SKIP_FILES:
            d = json.load(open(os.path.join(dd, f)))
            if isinstance(d, dict) and d.get("fruit") and "listings" in d: out[d["fruit"]] = d
    return out

def site_of(n):
    h = urlparse(n.get("url") or "").netloc.lower()
    return h[4:] if h.startswith("www.") else (h or "name:" + (n.get("name") or n["id"]).lower())

def batch_of(site): return "AB"[zlib.crc32(site.encode()) % 2]

def vendors(dd, want=None):
    data = load_dir(dd); sites = {}
    for fruit, d in data.items():
        N = {n["id"]: n for n in d["nurseries"]}
        for l in d["listings"]:
            n = N.get(l["nursery"]);
            if not n: continue
            s = sites.setdefault(site_of(n), {"names": set(), "fruits": {}})
            s["names"].add(n["name"]); f = s["fruits"].setdefault(fruit, {"nursery": n, "listings": []}); f["listings"].append(l)
    today = max((d.get("last_run") or {}).get("date") or d.get("updated") or "" for d in data.values())
    def age(x):
        try: return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(x)).days
        except Exception: return 999
    due, rest = [], []
    for site, s in sorted(sites.items()):
        if want and batch_of(site) != want: continue
        ls = [l for f in s["fruits"].values() for l in f["listings"]]
        live = sum(l["status"] == "in_stock" for l in ls)
        oldest = max(age(l.get("last_checked")) for l in ls)
        (due if live or oldest >= RECHECK_DAYS else rest).append((site, s, live, oldest))
    due.sort(key=lambda x: (-x[2], x[0]))
    print(f"# {len(due)} sites due tonight{' (batch ' + want + ')' if want else ''}; {len(rest)} skipped (nothing in stock, checked within {RECHECK_DAYS} days). Fruits loaded: {', '.join(data)}")
    print("# per listing: id | status | price ('+' = from-price) | last_checked | url path (same site unless a full url is shown)")
    for site, s, live, oldest in due:
        print(f"\n## {site} | {' / '.join(sorted(s['names']))} | {live} in stock")
        for fruit, f in sorted(s["fruits"].items()):
            n = f["nursery"]; sh = n.get("shipping") or {}
            print(f"[{fruit}] nursery id {n['id']} | shipping {sh.get('type')}{'*' if sh.get('confirmed') else ''}" + (f" | won't ship to {','.join(n['excluded_states'])}" if n.get("excluded_states") else ""))
            for l in sorted(f["listings"], key=lambda l: l["variety"]):
                u = l.get("url") or ""; pu = urlparse(u); hp = pu.netloc.lower().replace("www.", "")
                show = (pu.path + (("?" + pu.query) if pu.query else "")) if hp == site else u
                sale = f" (sale, was {l['was_price']})" if num(l.get("was_price")) else (" (sale)" if l.get("on_sale") else "")
                print(f"  {l['id']} | {l['status']} | {l.get('price')}{'+' if l.get('price_from') else ''}{sale} | {l.get('last_checked')} | {show}")
    if rest: print("\n## skipped tonight: " + ", ".join(site for site, *_ in rest))

# ---------------------------------------------------------------- alerts for the owner's watch list
def suspect_prices(d):
    """listing ids whose price shouldn't count as a deal: far below other sellers (a liner/plug or a misread page), or
    from a nursery whose pages haven't been readable for 2+ nights. Mirrors the board's rule."""
    N = {n["id"]: n for n in d["nurseries"]}; out = set(); by = {}
    for l in d["listings"]: by.setdefault(l["variety"], []).append(l)
    for ls in by.values():
        for l in ls:
            others = sorted(x["price"] for x in ls if x is not l and num(x.get("price")))
            k = len(others); med = None if k < 2 else (others[k // 2] if k % 2 else (others[k // 2 - 1] + others[k // 2]) / 2)
            if num(l.get("price")) and med and l["price"] < 0.3 * med and l["price"] < 0.5 * others[1]: out.add(l["id"])
            if ((N.get(l["nursery"]) or {}).get("unreadable") or {}).get("nights", 0) >= 2: out.add(l["id"])
    return out

def state_on(l, date):
    e = None
    for x in l.get("history") or []:
        if x.get("date", "") <= date: e = x
    return e

def lowest_on(d, vid, date, skip):
    best = None
    for l in d["listings"]:
        if l["variety"] != vid or l["id"] in skip: continue
        e = state_on(l, date)
        if e and e.get("status") == "in_stock" and num(e.get("price")) and (best is None or e["price"] < best[0]): best = (e["price"], l)
    return best

def alerts(dd, wp, date=None):
    """ALERT lines for tonight: a watched variety back in stock or newly listed in stock, or its lowest plant price
    newly under the owner's alert price. watch.json is the board's owner/state document (starred, targets)."""
    date = fix_date(date); data = load_dir(dd); w = json.load(open(wp))
    if isinstance(w.get("data"), dict): w = w["data"]
    targets = {k: v for k, v in (w.get("targets") or {}).items() if num(v)}
    keys = sorted(set(w.get("starred") or []) | set(targets))
    prev = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
    lines = []
    for key in keys:
        fruit, _, vid = key.partition(".")
        d = data.get(fruit)
        if not d: continue
        V = {v["id"]: v for v in d["varieties"]}; N = {n["id"]: n.get("name", n["id"]) for n in d["nurseries"]}
        if vid not in V: continue
        name = f"{V[vid]['name']} ({fruit})"; skip = suspect_prices(d)
        for l in d["listings"]:
            if l["variety"] != vid or l["id"] in skip: continue
            now, before = state_on(l, date), state_on(l, prev)
            if now and now.get("status") == "in_stock" and (before is None or before.get("status") != "in_stock") and now.get("date") == date:
                what = "newly listed" if before is None else "back in stock"
                lines.append(f"ALERT: {name} {what} at {N.get(l['nursery'], l['nursery'])}" + (f", {money(now['price'])} plus shipping" if num(now.get("price")) else "") + f" — {l.get('url') or ''}".rstrip(" —"))
        t = targets.get(key)
        if t is not None:
            lo_now, lo_prev = lowest_on(d, vid, date, skip), lowest_on(d, vid, prev, skip)
            if lo_now and lo_now[0] <= t and (lo_prev is None or lo_prev[0] > t):
                l = lo_now[1]
                lines.append(f"ALERT: {name} is {money(lo_now[0])} at {N.get(l['nursery'], l['nursery'])}, under your {money(t)} alert" + (f" (was {money(lo_prev[0])})" if lo_prev else "") + f" — {l.get('url') or ''}".rstrip(" —"))
    print(f"# {len(keys)} watched varieties, {len(targets)} with an alert price; date {date}")
    print("\n".join(dict.fromkeys(lines)) if lines else "NO ALERTS")

def apply_batch(dd, bp, od):
    data = load_dir(dd); b = json.load(open(bp)); date = fix_date(b.get("date"))
    os.makedirs(od, exist_ok=True); wrote, failed, stories = [], [], []
    for fruit, c in (b.get("fruits") or {}).items():
        if fruit not in data: print(f"REJECTED [{fruit}] no such fruit file"); failed.append(fruit); continue
        c = dict(c, date=date); out, r = apply(data[fruit], c); errs = validate(out, fruit)
        for m in r.applied: print(f"APPLIED  [{fruit}]", m)
        for m in r.held: print(f"HELD     [{fruit}]", m)
        for m in r.rejected: print(f"REJECTED [{fruit}]", m)
        if errs: print(f"NOT WRITTEN [{fruit}]: " + "; ".join(errs)); failed.append(fruit); continue
        json.dump(out, open(os.path.join(od, fruit + ".json"), "w"), indent=1, ensure_ascii=False); wrote.append(fruit)
        stories.append(f"{out.get('fruit', fruit).title()}: " + ((out.get("history_log") or [""])[-1].split(":", 1)[-1].strip() or "no changes"))
    if stories: print("\nSTORY (tonight so far, per fruit):\n" + "\n".join(stories))
    print(f"\nWROTE {', '.join(f'{od}/{f}.json' for f in wrote) or 'nothing'}" + (f"\nNOT WRITTEN: {', '.join(failed)} (fix and rerun; publish only the files listed as written)" if failed else ""))
    if failed: sys.exit(2)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "brief":
        brief(json.load(open(sys.argv[2])))
    elif cmd == "apply":
        d = json.load(open(sys.argv[2])); c = json.load(open(sys.argv[3]))
        c["date"] = fix_date(c.get("date"))
        out, r = apply(d, c)
        errs = validate(out, d["fruit"])
        for m in r.applied: print("APPLIED ", m)
        for m in r.held: print("HELD    ", m)
        for m in r.rejected: print("REJECTED", m)
        if errs:
            print("\nNOT WRITTEN: the result failed validation:\n  " + "\n  ".join(errs)); sys.exit(2)
        json.dump(out, open(sys.argv[4], "w"), indent=1, ensure_ascii=False)
        print(f"\nWROTE {sys.argv[4]}: {len(r.applied)} applied, {len(r.held)} held, {len(r.rejected)} rejected")
    elif cmd == "vendors":
        want = sys.argv[sys.argv.index("--batch") + 1].upper() if "--batch" in sys.argv else None
        vendors(sys.argv[2], want)
    elif cmd == "rotation":   # which fruit tonight's seller search covers: rotation DATE [fruit_rules.json]
        rp = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].endswith(".json") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fruit_rules.json")
        R = json.load(open(rp)); day = fix_date(sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "today" else None)
        print(f"# tonight = {day} (New York)"); i = (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(R["rotation_start"])).days % len(R["order"])
        f = R["order"][i]; print(json.dumps({"fruit": f, **R["fruits"][f]}, indent=1, ensure_ascii=False))
    elif cmd == "alerts":   # alerts DATA_DIR watch.json [DATE]
        alerts(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "apply-batch":
        apply_batch(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        sys.exit(__doc__)
