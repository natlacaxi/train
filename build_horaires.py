#!/usr/bin/env python3
"""
Génère horaires-paca.json : les départs ET arrivées TER de chaque gare du plan
(carte-ter.html), à partir du GTFS « horaires-sncf » de ressources.data.sncf.com.

Pourquoi un script ? Ce portail ne publie pas les horaires sous forme d'API
interrogeable, mais en fichiers GTFS (zip). Le script lit ce zip, garde les
départs des gares du plan et écrit un JSON léger que la page charge.

Usage :
  python3 build_horaires.py
  python3 build_horaires.py --gtfs mon_gtfs.zip --days 14
  python3 build_horaires.py --all-modes        # ne filtre pas sur TER

À relancer chaque jour (ou via un cron) : le fichier couvre N jours à partir
d'aujourd'hui.
"""
import argparse
import csv
import datetime as dt
import difflib
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile

GTFS_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"

# Zone PACA (large) : évite de confondre des gares homonymes ailleurs en France.
BBOX = (42.9, 46.0, 3.5, 8.0)  # lat min, lat max, lon min, lon max (PACA + Nîmes, Montpellier, Valence, Grenoble, Lyon)

# Si une gare n'est pas reconnue automatiquement, force le nom GTFS exact ici.
# Exemple : {"mar": "Marseille Saint-Charles"}
OVERRIDES = {}

WEEK = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
STOPWORDS = {"gare", "de", "du", "des", "la", "le", "les", "l", "d", "sur", "en", "lez", "et", "a", "aux"}


def unquote(s):
    return s[1:-1].replace("\\'", "'").replace('\\"', '"')


def stations_from_html(path):
    """Lit les gares (id -> nom) déclarées dans carte-ter.html."""
    src = open(path, encoding="utf-8").read()
    q = r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    out = {}
    for m in re.finditer(r"\bS\(\s*'(\w+)'\s*,\s*" + q, src):
        out[m[1]] = unquote(m[2])
    for m in re.finditer(r"\[\s*'(\w+)'\s*,\s*" + q, src):
        out[m[1]] = unquote(m[2])
    return out


def tokens(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"\bsaint(e?)\b", lambda m: "ste" if m[1] else "st", s)
    return [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in STOPWORDS]


def key(s):
    return " ".join(tokens(s))


def score(a, b):
    """Ressemblance entre deux noms de gare (0 à 1)."""
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    ta, tb = set(ka.split()), set(kb.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if small <= big and len(small) / len(big) >= 0.5:
        return 0.88
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def read_rows(zf, name):
    if name not in zf.namelist():
        return
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))


def open_gtfs(src):
    if re.match(r"https?://", src):
        print(f"Téléchargement de {src} …", file=sys.stderr)
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (compatible; carte-ter)"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return zipfile.ZipFile(io.BytesIO(r.read()))
    return zipfile.ZipFile(src)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=GTFS_URL, help="URL ou chemin du zip GTFS")
    ap.add_argument("--html", default="carte-ter.html")
    ap.add_argument("--out", default="horaires-paca.json")
    ap.add_argument("--days", type=int, default=14, help="jours couverts (30 max)")
    ap.add_argument("--all-modes", action="store_true", help="garder aussi TGV/Intercités")
    a = ap.parse_args()
    days = max(1, min(30, a.days))

    stations = stations_from_html(a.html)
    print(f"{len(stations)} gares lues dans {a.html}", file=sys.stderr)
    zf = open_gtfs(a.gtfs)

    # 1) Gares GTFS dans la zone, regroupées par nom normalisé
    stop_name, by_key = {}, {}
    for r in read_rows(zf, "stops.txt"):
        stop_name[r["stop_id"]] = r["stop_name"]
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except (ValueError, KeyError):
            continue
        if r.get("location_type", "") not in ("", "0", "1"):
            continue
        if BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]:
            g = by_key.setdefault(key(r["stop_name"]), {"name": r["stop_name"], "ids": set(), "pos": (lat, lon)})
            g["ids"].add(r["stop_id"])

    # 2) Rapprochement gare du plan <-> gare GTFS
    stop_to_map, matched, weak, missing, coords = {}, {}, [], [], {}
    for sid, name in stations.items():
        if sid in OVERRIDES:
            best = (1.0, by_key.get(key(OVERRIDES[sid])))
        else:
            variants = [name] + [p for p in name.split(" - ") if p.strip()]
            best = (0.0, None)
            for g in by_key.values():
                sc = max(score(v, g["name"]) for v in variants)
                if sc > best[0]:
                    best = (sc, g)
        if best[1] is None or best[0] < 0.82:
            missing.append(f"{sid} ({name})")
            continue
        matched[sid] = best[1]["name"]
        coords[sid] = [round(best[1]["pos"][0], 5), round(best[1]["pos"][1], 5)]
        for stop_id in best[1]["ids"]:
            stop_to_map.setdefault(stop_id, []).append(sid)
        if best[0] < 0.95:
            weak.append(f"{sid}: « {name} » -> « {best[1]['name']} » ({best[0]:.2f})")

    # 3) Jours de circulation des services sur la fenêtre
    start = dt.date.today()
    window = [start + dt.timedelta(i) for i in range(days)]
    full = (1 << days) - 1
    svc = {}
    cal = list(read_rows(zf, "calendar.txt"))
    for i, d in enumerate(window):
        ymd, wd = d.strftime("%Y%m%d"), WEEK[d.weekday()]
        for r in cal:
            if r["start_date"] <= ymd <= r["end_date"] and r.get(wd) == "1":
                svc[r["service_id"]] = svc.get(r["service_id"], 0) | (1 << i)
    index = {d.strftime("%Y%m%d"): i for i, d in enumerate(window)}
    for r in read_rows(zf, "calendar_dates.txt"):
        i = index.get(r["date"])
        if i is None:
            continue
        cur = svc.get(r["service_id"], 0)
        svc[r["service_id"]] = (cur | (1 << i)) if r["exception_type"] == "1" else (cur & ~(1 << i))

    # 4) Lignes TER (+ type de train pour l'affichage)
    def kind(txt):
        if re.search(r"OUIGO", txt, re.I): return "OUIGO"
        if re.search(r"INOUI", txt, re.I): return "INOUI"
        if re.search(r"\bTGV\b", txt, re.I): return "TGV"
        if re.search(r"INTERCIT", txt, re.I): return "INTERCITÉS"
        if re.search(r"\bTER\b", txt, re.I): return "TER"
        return ""
    routes = {}
    for r in read_rows(zf, "routes.txt"):
        txt = " ".join(r.get(c, "") for c in ("route_id", "route_short_name", "route_long_name", "route_desc"))
        routes[r["route_id"]] = (bool(re.search(r"\bTER\b", txt, re.I)), kind(txt))
    if not a.all_modes:
        kept = sum(1 for v in routes.values() if v[0])
        print(f"{kept}/{len(routes)} routes reconnues comme TER", file=sys.stderr)
        if kept == 0:
            print("Aucune route TER reconnue : repli sur toutes les routes (TGV/IC inclus).", file=sys.stderr)
            a.all_modes = True

    trips = {}
    for r in read_rows(zf, "trips.txt"):
        ister, typ = routes.get(r["route_id"], (False, ""))
        if not a.all_modes and not ister:
            continue
        if svc.get(r["service_id"], 0):
            num = (r.get("trip_short_name") or r.get("trip_headsign") or "").strip()
            if not typ:
                typ = kind(r["trip_id"] + " " + r.get("trip_headsign", ""))
            trips[r["trip_id"]] = (r["service_id"], num, typ, r["route_id"])

    # 5) Arrêts de chaque voyage (on garde tout : premier, dernier et gares du plan)
    rows = {}
    for r in read_rows(zf, "stop_times.txt"):
        tid = r["trip_id"]
        if tid not in trips:
            continue
        rows.setdefault(tid, []).append((
            int(r["stop_sequence"]), r["stop_id"],
            r.get("arrival_time") or r.get("departure_time") or "",
            r.get("departure_time") or r.get("arrival_time") or "",
            r.get("pickup_type", ""), r.get("drop_off_type", ""),
        ))

    def minutes(t):
        m = re.match(r"(\d+):(\d\d)", t or "")
        return int(m[1]) * 60 + int(m[2]) if m else None

    def spread(items, n=2):
        """n éléments répartis sur la liste (pour les « via »)."""
        if len(items) <= n:
            return items
        return [items[(i + 1) * len(items) // (n + 1)] for i in range(n)]

    out = {sid: {"d": {}, "a": {}} for sid in matched}
    tcount, unknown = {}, []
    for tid, rs in rows.items():
        rs.sort()
        service, num, typ, route_id = trips[tid]
        first, last = rs[0], rs[-1]
        if not typ:
            typ = kind(first[1])  # les ids d'arrêts SNCF contiennent le produit (OCEOUIGO, OCETGV INOUI, OCETrain TER…)
        if not typ:
            typ = kind(route_id)
        if not typ and len(re.sub(r"\D", "", num)) >= 5:
            typ = "TER"  # numéros à 5-6 chiffres = TER (repli quand la route n'indique pas le type)
        tcount[typ or "(non reconnu)"] = tcount.get(typ or "(non reconnu)", 0) + 1
        if not typ and len(unknown) < 6:
            unknown.append(f"trip_id={tid} route_id={route_id} 1er arrêt={first[1]} n°={num}")
        mapped = [x for x in rs if x[1] in stop_to_map]
        for idx, x in enumerate(mapped):
            seq, stop_id, arr, dep, pu, do = x
            for kind_, t, ok, ends, others in (
                ("d", dep, pu != "1" and seq != last[0], last, mapped[idx + 1:]),
                ("a", arr, do != "1" and seq != first[0], first, mapped[:idx]),
            ):
                total = minutes(t)
                if not ok or total is None:
                    continue
                mask = (svc[service] << (total // 1440)) & full
                if not mask:
                    continue
                cand = [stop_to_map[o[1]][0] for o in others if o[1] != ends[1] and o[1] != stop_id]
                via = spread(cand if kind_ == "d" else cand[::-1])
                if kind_ == "a":
                    via = via[::-1]
                name = stop_name.get(ends[1], "")
                for sid in stop_to_map[stop_id]:
                    k = (total % 1440, name, num, tuple(via), typ)
                    out[sid][kind_][k] = out[sid][kind_].get(k, 0) | mask

    def pack(d):
        return [[k[0], k[1], k[2], v, list(k[3]), k[4]] for k, v in sorted(d.items())]

    result = {
        "v": 2,
        "generated": start.isoformat(),
        "start": start.isoformat(),
        "days": days,
        "source": "SNCF Open Data (ODbL) - horaires-sncf",
        "coords": coords,
        "stopmap": {k: v for k, v in stop_to_map.items()},
        "stations": {sid: {"d": pack(v["d"]), "a": pack(v["a"])} for sid, v in out.items()},
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    nd = sum(len(v["d"]) for v in result["stations"].values())
    na = sum(len(v["a"]) for v in result["stations"].values())
    print(f"{len(matched)}/{len(stations)} gares rapprochées, {nd} départs, {na} arrivées -> {a.out}", file=sys.stderr)
    print("Types de trains : " + ", ".join(f"{k} {v}" for k, v in sorted(tcount.items(), key=lambda kv: -kv[1])), file=sys.stderr)
    if unknown:
        print("Exemples de trains au type non reconnu :\n  " + "\n  ".join(unknown), file=sys.stderr)
    if weak:
        print("\nRapprochements à vérifier :\n  " + "\n  ".join(weak), file=sys.stderr)
    if missing:
        print("\nGares sans correspondance (ajoute-les dans OVERRIDES) :\n  " + "\n  ".join(missing), file=sys.stderr)


if __name__ == "__main__":
    main()
  
