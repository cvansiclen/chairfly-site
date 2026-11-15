#!/usr/bin/env python3
"""
ChairFly Pro data builder.

Runs on GitHub Actions (see .github/workflows/chairfly-pro-data.yml) once a day. When a new FAA
28-day cycle is out, it downloads:
  - the d-TPP metafile (list of every approach plate + its PDF file name), and
  - the CIFP (FAA's coded procedures: fixes, coordinates, altitudes),
and writes small JSON files into the website repo under /pro/ for the iPad app:
  pro/meta.json          current cycle, effective/expiry dates, base URL for plate PDFs
  pro/airports.json      every airport that has at least one ChairFly-supported approach (search list)
  pro/apt/<ICAO>.json    one file per airport: its approaches, plate PDF names and the fixes to fly

Plate PDFs themselves are NOT copied — the app downloads them straight from aeronav.faa.gov.

Scope (Connor, 10/26/26): ILS, LOC, RNAV (GPS)/GPS, VOR, VOR/DME. Left out: NDB, LDA/SDF,
RNAV (RNP), TACAN/military, GLS, copter, CAT II/III and PRM duplicates. Military airports skipped.
Transitions that use DME arcs (AF legs) or RF legs are dropped.

Local test:  python3 build_pro_data.py --out ./site --dtpp metafile.xml --cifp FAACIFP18 --cycle 2612
"""
import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

AIRAC_BASE = dt.date(2020, 1, 2)  # AIRAC 2001


# --------------------------------------------------------------------------------------------
# AIRAC cycle math (same formula the app uses, see ProData.swift)
# --------------------------------------------------------------------------------------------
def airac_for(date):
    n = (date - AIRAC_BASE).days // 28
    eff = AIRAC_BASE + dt.timedelta(days=28 * n)
    first = eff
    while (first - dt.timedelta(days=28)).year == eff.year:
        first -= dt.timedelta(days=28)
    num = (eff - first).days // 28 + 1
    return eff, f"{eff.year % 100:02d}{num:02d}"


def airac_effective(ident):
    yy, nn = int(ident[:2]), int(ident[2:])
    d = dt.date(2000 + yy, 1, 1)
    eff, _ = airac_for(d + dt.timedelta(days=27))
    if eff.year < 2000 + yy:
        eff += dt.timedelta(days=28)
    return eff + dt.timedelta(days=28 * (nn - 1))


# --------------------------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------------------------
UA = {"User-Agent": "ChairFly-Pro-Data/1.0 (+https://chairflytrainer.com)"}


def fetch(url, timeout=300):
    print("GET", url, flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_first(urls):
    last = None
    for u in urls:
        try:
            return fetch(u), u
        except Exception as e:  # try the next spelling
            print("  failed:", e, flush=True)
            last = e
    raise RuntimeError(f"all download URLs failed: {last}")


def download_dtpp(cycle):
    return fetch_first([
        f"https://aeronav.faa.gov/d-tpp/{cycle}/xml_data/d-tpp_Metafile.xml",
        f"https://aeronav.faa.gov/d-tpp/{cycle}/xml_data/d-TPP_Metafile.xml",
        f"https://aeronav.faa.gov/d-tpp/{cycle}/xml_data/D-TPP_Metafile.xml",
    ])[0]


def download_cifp(eff):
    stamp = eff.strftime("%y%m%d")
    data, _ = fetch_first([
        f"https://aeronav.faa.gov/Upload_313-d/cifp/CIFP_{stamp}.zip",
        f"https://aeronav.faa.gov/Upload_313-d/cifp/cifp_{stamp}.zip",
    ])
    z = zipfile.ZipFile(io.BytesIO(data))
    name = next((n for n in z.namelist() if os.path.basename(n).upper().startswith("FAACIFP")), None)
    if not name:
        raise RuntimeError(f"no FAACIFP file in zip: {z.namelist()[:20]}")
    return z.read(name).decode("latin-1")


# --------------------------------------------------------------------------------------------
# d-TPP metafile
# --------------------------------------------------------------------------------------------
EXCLUDE_WORDS = ["NDB", "LDA", "SDF", "RNP", "TACAN", "GLS", "COPTER", "CAT II", "CAT III", "PRM",
                 "HI-", "HIGH", "SA CAT", "LOC BC", "BC RWY", "ADF", "VISUAL", "ARC", "DME/DME"]


def classify(chart_name):
    """Returns (type, runway, variant) or None. type is ILS / LOC / RNAV / VOR."""
    n = chart_name.upper().strip()
    if any(w in n for w in EXCLUDE_WORDS):
        return None
    if "CONT." in n:
        return None
    if n.startswith("ILS"):
        t = "ILS"
    elif n.startswith("LOC"):
        t = "LOC"
    elif n.startswith("RNAV (GPS)") or n.startswith("GPS"):
        t = "RNAV"
    elif n.startswith("VOR"):
        t = "VOR"
    else:
        return None
    m = re.search(r"RWY\s+(\d{1,2}[LRC]?)", n)
    runway = m.group(1).zfill(2) if m else None
    if runway is None:
        m = re.search(r"-([A-Z])\b", n)  # circling, e.g. VOR-A
        variant = m.group(1) if m else None
    else:
        # variant letter before RWY, e.g. "RNAV (GPS) Y RWY 27" / "ILS Z OR LOC Z RWY 9"
        m = re.search(r"\b([W-Z])\s+(?:OR\s|RWY)", n)
        variant = m.group(1) if m else None
    return t, runway, variant


def parse_dtpp(xml_bytes):
    root = ET.fromstring(xml_bytes)
    cycle = root.get("cycle") or root.get("Cycle")
    airports = {}
    for state in root.iter("state_code"):
        st = state.get("ID", "")
        for city in state.iter("city_name"):
            city_name = city.get("ID", "")
            for apt in city.iter("airport_name"):
                if (apt.get("military") or "N").upper() == "Y":
                    continue
                icao = (apt.get("icao_ident") or "").strip()
                faa = (apt.get("apt_ident") or "").strip()
                if not icao:
                    icao = faa  # no ICAO code (e.g. French Valley): the app keys it by FAA ID, "F70"
                if not icao:
                    continue
                procs, last = [], None
                for rec in apt.iter("record"):
                    code = (rec.findtext("chart_code") or "").strip()
                    if code != "IAP":
                        continue
                    if (rec.findtext("useraction") or "").strip().upper() == "D":
                        continue
                    name = (rec.findtext("chart_name") or "").strip()
                    pdf = (rec.findtext("pdf_name") or "").strip()
                    if not pdf:
                        continue
                    if "CONT." in name.upper() and last is not None:
                        last["pdfs"].append(pdf)
                        continue
                    c = classify(name)
                    if not c:
                        last = None
                        continue
                    t, rwy, var = c
                    last = {
                        "name": name, "type": t, "runway": rwy, "variant": var, "pdfs": [pdf],
                        "amdt": (rec.findtext("amdtnum") or "").strip(),
                        "amdtDate": (rec.findtext("amdtdate") or "").strip(),
                    }
                    procs.append(last)
                if procs:
                    airports[icao] = {"icao": icao, "faa": faa, "name": apt.get("ID", "").title(),
                                      "city": city_name.title(), "state": st, "procs": procs}
    return cycle, airports


# --------------------------------------------------------------------------------------------
# CIFP (ARINC 424 v18, 132-char fixed records; 0-based slices)
# --------------------------------------------------------------------------------------------
def parse_lat(s):
    s = s.strip()
    if len(s) != 9 or s[0] not in "NS":
        return None
    v = int(s[1:3]) + int(s[3:5]) / 60 + int(s[5:9]) / 100 / 3600
    return -v if s[0] == "S" else v


def parse_lon(s):
    s = s.strip()
    if len(s) != 10 or s[0] not in "EW":
        return None
    v = int(s[1:4]) + int(s[4:6]) / 60 + int(s[6:10]) / 100 / 3600
    return -v if s[0] == "W" else v


def parse_magvar(s):
    """'E0120' -> +12.0 (east), 'W0120' -> -12.0."""
    s = s.strip()
    if len(s) != 5 or s[0] not in "EWT" or not s[1:].isdigit():
        return 0.0
    if s[0] == "T":
        return 0.0
    v = int(s[1:]) / 10
    return v if s[0] == "E" else -v


def parse_int(s):
    s = s.strip()
    return int(s) if s.lstrip("-").isdigit() else None


def parse_alt(s):
    s = s.strip()
    if s.startswith("FL") and s[2:].isdigit():
        return int(s[2:]) * 100
    return int(s) if s.isdigit() else None


TARGET_TYPES = {"I": "ILS", "L": "LOC", "R": "RNAV", "P": "RNAV", "V": "VOR", "D": "VOR", "S": "VOR"}


def parse_cifp(text, wanted):
    """wanted: dict of CIFP airport ident -> our airport key (see build()). Returns dict of lookup tables."""
    T = {"apt": {}, "rwy": {}, "term": {}, "enr": {}, "vhf": {}, "ndb": {}, "loc": {}, "legs": {}}
    for line in text.splitlines():
        if len(line) < 123 or line[0] != "S":
            continue
        sec = line[4]
        if sec == "P":
            icao = wanted.get(line[6:10].strip())
            if icao is None:
                continue
            sub = line[12]
            if sub == "F":
                if line[38] not in "01":
                    continue  # continuation records
                pid = line[13:19].strip()
                T["legs"].setdefault((icao, pid), []).append({
                    "route": line[19], "trans": line[20:25].strip(), "seq": parse_int(line[26:29]) or 0,
                    "fix": line[29:34].strip(), "fixSec": line[36:38], "desc": line[39:43],
                    "turn": line[43], "pt": line[47:49], "vhf": line[50:54].strip(),
                    "course": parse_int(line[70:74]), "altDesc": line[82].strip(),
                    "alt1": parse_alt(line[84:89]), "alt2": parse_alt(line[89:94]),
                    "vert": parse_int(line[102:106]),
                })
                continue
            if line[21] not in "01":
                continue
            if sub == "A":
                T["apt"][icao] = {"lat": parse_lat(line[32:41]), "lon": parse_lon(line[41:51]),
                                  "var": parse_magvar(line[51:56]), "elev": parse_int(line[56:61]) or 0}
            elif sub == "G":
                rid = line[13:18].strip()
                brg = line[27:31].strip()
                T["rwy"][(icao, rid)] = {"lat": parse_lat(line[32:41]), "lon": parse_lon(line[41:51]),
                                         "brg": int(brg) / 10 if brg.isdigit() else None,
                                         "elev": parse_int(line[66:71]), "tch": parse_int(line[75:77])}
            elif sub == "C":
                T["term"][(icao, line[13:18].strip())] = (parse_lat(line[32:41]), parse_lon(line[41:51]))
            elif sub == "I":
                brg = line[51:55].strip()
                T["loc"][(icao, line[13:17].strip())] = {
                    "lat": parse_lat(line[32:41]), "lon": parse_lon(line[41:51]),
                    "brg": int(brg) / 10 if brg.isdigit() else None, "rwy": line[27:32].strip(),
                    "gs": parse_int(line[87:90])}
            elif sub == "N":
                T["ndb"].setdefault(line[13:17].strip(), (parse_lat(line[32:41]), parse_lon(line[41:51])))
        elif sec == "E" and line[5] == "A":
            if line[21] not in "01":
                continue
            T["enr"].setdefault(line[13:18].strip(), (parse_lat(line[32:41]), parse_lon(line[41:51])))
        elif sec == "D":
            if line[21] not in "01":
                continue
            ident = line[13:17].strip()
            lat, lon = parse_lat(line[32:41]), parse_lon(line[41:51])
            if lat is None:
                lat, lon = parse_lat(line[55:64]), parse_lon(line[64:74])
            if lat is None:
                continue
            if line[5] == " ":
                T["vhf"].setdefault(ident, (lat, lon))
            elif line[5] == "B":
                T["ndb"].setdefault(ident, (lat, lon))
    return T


def fix_position(T, icao, fix, fix_sec):
    if fix_sec == "PC":
        p = T["term"].get((icao, fix))
    elif fix_sec == "PG":
        r = T["rwy"].get((icao, fix))
        p = (r["lat"], r["lon"]) if r else None
    elif fix_sec[0] == "D" and fix_sec[1] == " ":
        p = T["vhf"].get(fix)
    elif fix_sec in ("DB", "PN"):
        p = T["ndb"].get(fix)
    elif fix_sec == "EA":
        p = T["enr"].get(fix)
    elif fix_sec == "PI":
        l = T["loc"].get((icao, fix))
        p = (l["lat"], l["lon"]) if l else None
    else:
        p = None
    if p is None or p[0] is None:
        p = T["term"].get((icao, fix)) or T["enr"].get(fix) or T["vhf"].get(fix) or T["ndb"].get(fix)
    return p if p and p[0] is not None else None


# --------------------------------------------------------------------------------------------
# Geometry: lat/lon -> east/north NM in a MAGNETIC-north frame around the airport, so headings
# and courses in the sim read the same as on the plate.
# --------------------------------------------------------------------------------------------
def to_local(lat, lon, ref):
    n_t = (lat - ref["lat"]) * 60.0
    e_t = (lon - ref["lon"]) * 60.0 * math.cos(math.radians(ref["lat"]))
    v = math.radians(ref["var"])
    n_m = n_t * math.cos(v) + e_t * math.sin(v)
    e_m = e_t * math.cos(v) - n_t * math.sin(v)
    return round(e_m, 3), round(n_m, 3)


def bearing(a, b):
    return (math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) + 360) % 360


ROLE = {"A": "IAF", "B": "IF", "C": "IAF", "D": "IAF", "I": "IF", "F": "FAF", "M": "MAP"}


def leg_points(T, icao, ref, legs):
    out = []
    for lg in legs:
        if not lg["fix"]:
            continue
        p = fix_position(T, icao, lg["fix"], lg["fixSec"])
        if not p:
            continue
        e, n = to_local(p[0], p[1], ref)
        out.append({"id": lg["fix"], "e": e, "n": n, "alt": lg["alt1"], "alt2": lg["alt2"],
                    "altDesc": lg["altDesc"] or "@", "role": ROLE.get(lg["desc"][3], ""),
                    "hold": lg["pt"] in ("HF", "HM", "HA"), "navaid": lg["fixSec"] == "D "})
    # collapse consecutive duplicates (hold-in-lieu re-lists the fix)
    dedup = []
    for p in out:
        if dedup and dedup[-1]["id"] == p["id"]:
            if dedup[-1]["alt"] is None:
                dedup[-1]["alt"] = p["alt"]
            continue
        dedup.append(p)
    return dedup


def match_cifp(T, icao, proc):
    """Find the CIFP procedure id for a d-TPP plate (e.g. 'ILS RWY 27' -> 'I27')."""
    letters = {"ILS": "I", "LOC": "L", "RNAV": "RP", "VOR": "VDS"}[proc["type"]]
    rwy, var = proc["runway"], proc["variant"]
    best = None
    for (a, pid) in T["legs"].keys():
        if a != icao or pid[0] not in letters:
            continue
        rest = pid[1:]
        if rwy:
            m = re.match(r"^(\d{2}[LRC]?)(?:-?([A-Z]))?$", rest)
            if not m or m.group(1) != rwy:
                continue
            if (m.group(2) or None) != var:
                continue
        else:
            m = re.match(r"^(?:DM|M)?-?([A-Z])$", rest)
            if not m or m.group(1) != var:
                continue
        # prefer the plate's own letter order (e.g. ILS before LOC, RNAV 'R' before GPS 'P')
        rank = letters.index(pid[0])
        if best is None or rank < best[0]:
            best = (rank, pid)
    return best[1] if best else None


def build_procedure(T, icao, ref, proc):
    pid = match_cifp(T, icao, proc)
    out = {"name": proc["name"], "type": proc["type"], "runway": proc["runway"],
           "pdfs": proc["pdfs"], "amdt": proc["amdt"], "amdtDate": proc["amdtDate"], "flyable": False}
    if not pid:
        return out
    legs = sorted(T["legs"][(icao, pid)], key=lambda l: (l["route"], l["trans"], l["seq"]))
    finals = [l for l in legs if l["route"] != "A"]
    trans = [l for l in legs if l["route"] == "A"]
    if not finals:
        return out
    # final approach = legs up to and including the MAP; everything after is the missed approach
    route_key = finals[0]["route"]
    finals = [l for l in finals if l["route"] == route_key]
    finals.sort(key=lambda l: l["seq"])
    map_idx = next((i for i, l in enumerate(finals) if l["desc"][3] == "M"), None)
    if map_idx is None:
        map_idx = next((i for i, l in enumerate(finals) if l["fixSec"] == "PG"), len(finals) - 1)
    final_legs, missed_legs = finals[:map_idx + 1], finals[map_idx + 1:]
    fixes = leg_points(T, icao, ref, final_legs)
    if len(fixes) < 2:
        return out

    rwy = T["rwy"].get((icao, "RW" + proc["runway"])) if proc["runway"] else None
    if rwy and rwy["lat"] is not None:
        te, tn = to_local(rwy["lat"], rwy["lon"], ref)
        thr = {"e": te, "n": tn, "elev": rwy["elev"] if rwy["elev"] is not None else ref["elev"],
               "tch": rwy["tch"] or 50}
        course = rwy["brg"]
    else:  # circling: aim at the MAP / airport
        last = fixes[-1]
        thr = {"e": last["e"], "n": last["n"], "elev": ref["elev"], "tch": 50}
        course = None
    # final course from the FAF (or second-to-last fix) to the threshold, in the magnetic frame
    faf = next((f for f in fixes if f["role"] == "FAF"), fixes[-2] if len(fixes) > 1 else None)
    geo_course = None
    if faf and (faf["e"], faf["n"]) != (thr["e"], thr["n"]):
        geo_course = round(bearing((faf["e"], faf["n"]), (thr["e"], thr["n"])), 1)
    # the fix geometry is what the sim actually flies, so its course wins over the published bearing
    if geo_course is not None:
        course = geo_course
    if course is None:
        return out

    vert = next((l["vert"] for l in reversed(final_legs) if l["vert"]), None)
    gpa = abs(vert) / 100 if vert else None

    result = dict(out)
    result.update({
        "flyable": True, "cifpId": pid, "finalCourse": round(course, 1), "threshold": thr,
        "gpaDeg": gpa, "fixes": fixes,
        "missed": [{"id": p["id"], "e": p["e"], "n": p["n"], "alt": p["alt"]}
                   for p in leg_points(T, icao, ref, missed_legs) if not p.get("hold")][:4],
        "missedAlt": max([l["alt1"] for l in missed_legs if l["alt1"]] or [0]) or None,
    })
    if proc["type"] in ("ILS", "LOC"):
        loc = next((v for (a, _), v in T["loc"].items() if a == icao and v["rwy"] == "RW" + (proc["runway"] or "")), None)
        if loc and loc["lat"] is not None:
            le, ln = to_local(loc["lat"], loc["lon"], ref)
            result["localizer"] = {"e": le, "n": ln}
            if proc["type"] == "ILS" and loc["gs"]:
                result["gpaDeg"] = loc["gs"] / 100
        if proc["type"] == "ILS" and not result["gpaDeg"]:
            result["gpaDeg"] = 3.0
    if proc["type"] == "VOR":
        nav = next((l["vhf"] for l in final_legs if l["vhf"]), None)
        nav = nav or next((l["fix"] for l in final_legs if l["fixSec"] == "D "), None)
        p = T["vhf"].get(nav) if nav else None
        if not p:
            result["flyable"] = False
        else:
            ve, vn = to_local(p[0], p[1], ref)
            result["navaid"] = {"id": nav, "e": ve, "n": vn}
    # transitions: just map dots (skip DME arcs / RF legs per scope)
    tmap = {}
    for l in trans:
        tmap.setdefault(l["trans"], []).append(l)
    tr_out = []
    for name, tl in sorted(tmap.items()):
        if any(l["pt"] in ("AF", "RF") for l in tl):
            continue
        pts = [p for p in leg_points(T, icao, ref, sorted(tl, key=lambda l: l["seq"])) if not p.get("hold")]
        if pts:
            tr_out.append({"id": name, "fixes": [{"id": p["id"], "e": p["e"], "n": p["n"], "alt": p["alt"], "navaid": p["navaid"]} for p in pts]})
    result["transitions"] = tr_out
    return result


# --------------------------------------------------------------------------------------------
def build(out_dir, cycle, dtpp_bytes, cifp_text):
    dtpp_cycle, airports = parse_dtpp(dtpp_bytes)
    cycle = cycle or dtpp_cycle
    print(f"d-TPP: {len(airports)} airports with supported approaches", flush=True)
    # CIFP may spell an airport as its ICAO code, its FAA ID, or K + FAA ID — accept all three
    aliases = {}
    for key, a in airports.items():
        for alias in (key, a["faa"], "K" + a["faa"] if a["faa"] else None):
            if alias:
                aliases.setdefault(alias, key)
    T = parse_cifp(cifp_text, aliases)
    print(f"CIFP: {len(T['apt'])} airports, {len(T['legs'])} procedures", flush=True)

    eff = airac_effective(cycle)
    pro = os.path.join(out_dir, "pro")
    apt_dir = os.path.join(pro, "apt")
    os.makedirs(apt_dir, exist_ok=True)
    for f in os.listdir(apt_dir):  # last cycle's files
        os.remove(os.path.join(apt_dir, f))

    index, flyable, total = [], 0, 0
    for icao in sorted(airports):
        a = airports[icao]
        ref = T["apt"].get(icao)
        if not ref or ref["lat"] is None:
            ref = None
        procs = []
        for p in a["procs"]:
            total += 1
            bp = build_procedure(T, icao, ref, p) if ref else {
                "name": p["name"], "type": p["type"], "runway": p["runway"], "pdfs": p["pdfs"],
                "amdt": p["amdt"], "amdtDate": p["amdtDate"], "flyable": False}
            flyable += bp["flyable"]
            procs.append(bp)
        doc = {"icao": icao, "faa": a["faa"], "name": a["name"], "city": a["city"], "state": a["state"],
               "cycle": cycle, "elevFt": ref["elev"] if ref else 0, "magVar": ref["var"] if ref else 0,
               "procedures": procs}
        with open(os.path.join(apt_dir, f"{icao}.json"), "w") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        index.append([icao, a["faa"], a["name"], a["city"], a["state"], len(procs)])

    with open(os.path.join(pro, "airports.json"), "w") as fh:
        json.dump({"cycle": cycle, "airports": index}, fh, separators=(",", ":"))
    meta = {"cycle": cycle, "effective": eff.isoformat(),
            "expires": (eff + dt.timedelta(days=28)).isoformat(),
            "plateBaseURL": f"https://aeronav.faa.gov/d-tpp/{cycle}/",
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "airportCount": len(index), "approachCount": total, "flyableCount": flyable}
    with open(os.path.join(pro, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    print(json.dumps(meta, indent=1))
    if total and flyable / total < 0.5:
        print("WARNING: fewer than half the approaches matched CIFP data — check the parser", flush=True)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--cycle", help="force a cycle like 2612 (default: current AIRAC)")
    ap.add_argument("--dtpp", help="local d-TPP metafile (testing)")
    ap.add_argument("--cifp", help="local FAACIFP18 file (testing)")
    ap.add_argument("--force", action="store_true", help="rebuild even if pro/meta.json is current")
    args = ap.parse_args()

    eff, cycle = airac_for(dt.date.today())
    cycle = args.cycle or cycle
    eff = airac_effective(cycle)
    meta_path = os.path.join(args.out, "pro", "meta.json")
    if not args.force and not args.dtpp and os.path.exists(meta_path):
        with open(meta_path) as fh:
            if json.load(fh).get("cycle") == cycle:
                print(f"pro data already on cycle {cycle}; nothing to do")
                return 0
    dtpp = open(args.dtpp, "rb").read() if args.dtpp else download_dtpp(cycle)
    cifp = open(args.cifp, encoding="latin-1").read() if args.cifp else download_cifp(eff)
    build(args.out, cycle, dtpp, cifp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
