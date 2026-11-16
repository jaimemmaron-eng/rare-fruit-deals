#!/usr/bin/env python3
"""Shipping-by-ZIP model for Rare Fruit Deals.

data/shipping.json holds one record per nursery (keyed by website domain, or by
name for nurseries without their own site), with:
  ship_from {city, state, zip}, rule (how they charge), excluded_states,
  season, carrier, confidence, source, checked.

estimate(rec, dest_zip, subtotal, items=1) -> dict
  {ok: bool, lo, hi, how, zone, blocked: reason|None}
  ok=True  -> the nursery publishes the rule, so the number is exact for that order
  ok=False -> estimated from distance (carrier zone) because rates show only at checkout

Distance model: destination ZIP -> 3-digit prefix -> state (or a sub-region of a
large state) -> approximate centre point. Origin uses the same table from its ZIP,
or its state centre. Great-circle miles -> USPS/UPS-style zone 1-8 (AK/HI = 9).
Good to about +/- 1 zone, which is the right precision for a "~$" estimate.

CLI:
  python3 shipping.py build  merged_raw.json all_nurseries.json  > shipping.json
  python3 shipping.py quote  shipping.json  <dest_zip> [subtotal] [items]
"""
import json, math, sys

# ---------------------------------------------------------------- ZIP3 -> region
# (first zip3, last zip3, region) — region is a state code or STATE-subregion
ZIP3 = [
    (5, 5, "NY-NYC"), (6, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
    (50, 54, "VT"), (55, 55, "MA"), (56, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"),
    (100, 119, "NY-NYC"), (120, 149, "NY-UP"), (150, 168, "PA-W"), (169, 196, "PA-E"), (197, 199, "DE"),
    (200, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"), (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"),
    (300, 319, "GA"), (320, 329, "FL-N"), (330, 349, "FL-S"), (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"),
    (398, 399, "GA"), (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"), (500, 528, "IA"),
    (530, 549, "WI"), (550, 567, "MN"), (569, 569, "DC"), (570, 577, "SD"), (580, 588, "ND"), (590, 599, "MT"),
    (600, 629, "IL"), (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"), (716, 729, "AR"),
    (730, 732, "OK"), (733, 733, "TX-C"), (734, 749, "OK"), (750, 769, "TX-N"), (770, 779, "TX-H"), (780, 789, "TX-C"),
    (790, 799, "TX-W"), (800, 816, "CO"), (820, 831, "WY"), (832, 838, "ID"), (840, 847, "UT"), (850, 865, "AZ"),
    (870, 884, "NM"), (885, 885, "TX-W"), (889, 898, "NV"), (900, 935, "CA-S"), (936, 961, "CA-N"),
    (967, 968, "HI"), (970, 976, "OR-W"), (977, 979, "OR-E"), (980, 986, "WA-W"), (988, 994, "WA-E"), (995, 999, "AK"),
]
# approximate centre (lat, lon) per region, weighted toward where people live
CENTRE = {
    "AL": (33.0, -86.8), "AK": (61.2, -149.9), "AZ": (33.5, -111.9), "AR": (35.0, -92.4), "CA-N": (38.0, -121.6),
    "CA-S": (34.1, -117.9), "CO": (39.7, -105.0), "CT": (41.6, -72.7), "DE": (39.2, -75.6), "DC": (38.9, -77.0),
    "FL-N": (29.8, -82.5), "FL-S": (27.0, -81.0), "GA": (33.3, -84.0), "HI": (21.3, -157.8), "ID": (43.8, -115.6),
    "IL": (41.2, -88.6), "IN": (39.9, -86.3), "IA": (41.9, -93.4), "KS": (38.5, -97.0), "KY": (37.8, -85.3),
    "LA": (30.8, -91.6), "ME": (44.4, -69.8), "MD": (39.1, -76.8), "MA": (42.3, -71.6), "MI": (42.8, -84.4),
    "MN": (45.3, -93.6), "MS": (32.6, -89.7), "MO": (38.4, -92.2), "MT": (46.6, -110.5), "NE": (41.0, -97.2),
    "NV": (36.6, -115.5), "NH": (43.2, -71.5), "NJ": (40.4, -74.5), "NM": (34.8, -106.4), "NY-NYC": (40.8, -73.9),
    "NY-UP": (42.9, -75.8), "NC": (35.6, -79.5), "ND": (47.2, -99.0), "OH": (40.4, -82.7), "OK": (35.6, -96.9),
    "OR-W": (44.9, -122.9), "OR-E": (44.2, -119.5), "PA-E": (40.4, -76.0), "PA-W": (40.6, -79.6), "PR": (18.3, -66.4),
    "RI": (41.8, -71.4), "SC": (34.0, -81.0), "SD": (44.0, -98.9), "TN": (35.8, -86.4), "TX-N": (32.7, -96.8),
    "TX-H": (29.8, -95.4), "TX-C": (29.9, -97.9), "TX-W": (32.0, -102.0), "UT": (40.6, -111.9), "VT": (44.3, -72.8),
    "VA": (37.8, -77.9), "WA-W": (47.4, -122.3), "WA-E": (47.2, -118.6), "WV": (38.6, -80.9), "WI": (43.9, -89.2),
    "WY": (42.6, -107.3),
}
STATE_REGION = {"NY": "NY-UP", "PA": "PA-E", "FL": "FL-N", "TX": "TX-N", "CA": "CA-N", "OR": "OR-W", "WA": "WA-W"}
LOWER48_ONLY = {"AK", "HI", "PR"}

def region_of_zip(z):
    try: p = int(str(z).strip()[:3])
    except (ValueError, TypeError): return None
    for a, b, r in ZIP3:
        if a <= p <= b: return r
    return None

def state_of(region): return region.split("-")[0] if region else None

def region_of_state(st): return STATE_REGION.get(st, st) if st in CENTRE or st in STATE_REGION else None

def miles(a, b):
    (la1, lo1), (la2, lo2) = CENTRE[a], CENTRE[b]
    p1, p2, dl, dn = map(math.radians, (la1, la2, la2 - la1, lo2 - lo1))
    h = math.sin(dl / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dn / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(h))

ZONE_MILES = [(50, 1), (150, 2), (300, 3), (600, 4), (1000, 5), (1400, 6), (1800, 7)]
def zone(o_region, d_region):
    if not o_region or not d_region: return 5          # unknown origin: assume mid-country
    if state_of(d_region) in ("AK", "HI", "PR") and state_of(o_region) != state_of(d_region): return 9
    if o_region == d_region: return 2
    m = miles(o_region, d_region)
    for lim, z in ZONE_MILES:
        if m <= lim: return max(z, 2)
    return 8

# one small box of plants (bare-root or 1-gal, ~5-10 lb) by ground, incl. typical nursery handling
GENERIC = {2: (12, 18), 3: (13, 20), 4: (14, 22), 5: (16, 25), 6: (18, 28), 7: (19, 31), 8: (21, 34), 9: (35, 60)}
FACTOR = {z: (lo + hi) / 2 / 15 for z, (lo, hi) in GENERIC.items()}   # relative cost by zone

# ---------------------------------------------------------------- applying a rule
def _table(rule, x):
    for mx, cost in rule.get("table", []):
        if x <= mx: return cost
    if rule.get("over_amount") is not None: return rule["over_amount"]
    if rule.get("over_pct") is not None: return round(x * rule["over_pct"], 2)
    return rule["table"][-1][1] if rule.get("table") else None

def _apply(rule, d_state, subtotal, items, z, rec):
    t = rule.get("type")
    if rule.get("free_over") is not None and subtotal >= rule["free_over"]: return (0, 0, True, "free")
    if t == "flat": return (rule["amount"], rule["amount"], True, "flat")
    if t == "included": return (0, 0, True, "incl")
    if t == "pickup": return (0, 0, True, "pickup")
    if t == "table":
        v = _table(rule, items if rule.get("basis") == "items" else subtotal)
        if v is None: return None
        for s in rule.get("surcharges", []):
            if d_state in s["states"]: v += s.get("add", 0) + subtotal * s.get("pct", 0)
        return (round(v, 2), round(v, 2), True, "table")
    if t == "percent":
        pct = next((p for mx, p in rule["tiers"] if subtotal <= mx), rule["tiers"][-1][1])
        v = max(rule.get("min", 0), subtotal * pct) + rule.get("flat_plus", 0)
        return (round(v, 2), round(v, 2), True, "pct")
    if t == "per_item":
        v = rule["first_item"] + rule.get("each_additional", rule["first_item"]) * max(0, items - 1)
        return (v, v, True, "per_item")
    if t == "region":
        default = None
        for r in rule["regions"]:
            if d_state in r.get("states", []): return _apply(r["rule"], d_state, subtotal, items, z, rec)
            if not r.get("states") or r["states"] == ["*"]: default = r
        if default: return _apply(default["rule"], d_state, subtotal, items, z, rec)
        if rule.get("else"): return _apply(rule["else"], d_state, subtotal, items, z, rec)
        return None
    return None   # calculated

def estimate(rec, dest_zip, subtotal=0.0, items=1, listing_est=None):
    """listing_est: optional [lo, hi] a listing already carries for the board's home ZIP (08801)."""
    d_region = region_of_zip(dest_zip)
    d_state = state_of(d_region)
    sf = rec.get("ship_from") or {}
    o_region = region_of_zip(sf.get("zip")) if sf.get("zip") else region_of_state(sf.get("state"))
    z = zone(o_region, d_region)
    out = {"zone": z, "blocked": None}
    if not rec.get("ships", True):
        return {**out, "ok": True, "lo": 0, "hi": 0, "how": "pickup", "blocked": "pickup only"}
    excl = set(rec.get("excluded_states") or [])
    if d_state in excl:
        return {**out, "ok": False, "lo": None, "hi": None, "how": "blocked", "blocked": f"doesn't ship to {d_state}"}
    if d_state in LOWER48_ONLY and rec.get("lower48_only"):
        return {**out, "ok": False, "lo": None, "hi": None, "how": "blocked", "blocked": "ships to the lower 48 only"}
    r = _apply(rec["rule"], d_state, subtotal, items, z, rec)
    if r:
        lo, hi, ok, how = r
        return {**out, "ok": ok, "lo": lo, "hi": hi, "how": how}
    # calculated at checkout: scale a known estimate by zone, else the generic box rate
    rule = rec["rule"]
    if listing_est:
        home = zone(o_region, region_of_zip(HOME_ZIP))
        f = FACTOR[z] / FACTOR[home]
        lo, hi = listing_est[0] * f, listing_est[1] * f
    else:
        lo, hi = GENERIC[z]
        extra = max(0, items - 1)
        lo, hi = lo + 2 * extra, hi + 5 * extra
    if rule.get("range_hint"):          # what the nursery itself says, e.g. "about $20-25"
        a, b = rule["range_hint"]
        lo, hi = min(max(lo, a), b), min(max(hi, a), b)
    if rule.get("free_over") is not None and subtotal >= rule["free_over"]:
        return {**out, "ok": True, "lo": 0, "hi": 0, "how": "free"}
    return {**out, "ok": False, "lo": round(lo), "hi": round(hi), "how": "est"}

HOME_ZIP = "08801"

# ---------------------------------------------------------------- build
def build(raw, nurseries):
    ids = {g["key"]: g["ids"] for g in nurseries}
    out = {}
    for key, r in raw.items():
        sf = dict(r.get("ship_from") or {})
        if sf.get("state") and len(sf["state"]) != 2: sf["state"] = None
        excl = sorted({s for s in (r.get("excluded_states") or []) if len(s) == 2})
        rec = {
            "name": r["name"], "board_ids": ids.get(key, []), "ships": r.get("ships", True),
            "ship_from": {k: sf.get(k) for k in ("city", "state", "zip", "zip_source")},
            "rule": r["rule"], "excluded_states": excl, "season": r.get("season"),
            "carrier": r.get("carrier"), "confidence": r.get("confidence"),
            "source": r.get("source"), "checked": r.get("checked", "2026-10-05"), "notes": r.get("notes"),
        }
        out[key] = rec
    return out

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        raw, nur = json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
        json.dump(build(raw, nur), sys.stdout, indent=1)
    elif cmd == "quote":
        data = json.load(open(sys.argv[2]))["nurseries"]
        z, sub = sys.argv[3], float(sys.argv[4]) if len(sys.argv) > 4 else 40
        items = int(sys.argv[5]) if len(sys.argv) > 5 else 1
        for k, rec in sorted(data.items()):
            e = estimate(rec, z, sub, items)
            print(f"{k[:30]:30} z{e['zone']} {e['how']:8} {'' if e['ok'] else '~'}{e['lo']}-{e['hi']} {e['blocked'] or ''}")
    else:
        print(__doc__)
