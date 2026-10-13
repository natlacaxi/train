#!/usr/bin/env python3
"""
Génère les fichiers d'horaires de la carte (index.html) à partir du GTFS
« horaires-sncf » de ressources.data.sncf.com :
  - horaires-paca.json    départs ET arrivées de chaque gare du plan (PACA + axe Lyon - Paris)
  - horaires-france.json  tous les trains de France, pour la recherche de trajet locale
  - stops-france.json     table id d'arrêt GTFS -> gare (pour le workflow des incidents)

Pourquoi un script ? Ce portail ne publie pas les horaires sous forme d'API
interrogeable, mais en fichiers GTFS (zip). Le script lit ce zip, garde les
départs des gares du plan et écrit un JSON léger que la page charge.

Usage :
  python3 build_horaires.py
  python3 build_horaires.py --gtfs mon_gtfs.zip --days 14
  python3 build_horaires.py --ter-only         # ne garde que les TER dans horaires-paca.json

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
import math
import time
import urllib.request
import zipfile

GTFS_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"

# Zones de recherche des gares du plan (évite de confondre des gares homonymes ailleurs en France).
# lat min, lat max, lon min, lon max
BBOX = (42.9, 46.0, 3.5, 8.0)       # PACA + Nîmes, Montpellier, Valence, Grenoble, Lyon
BBOX_EXT = (45.0, 49.3, 1.8, 6.0)   # couloir Lyon - Dijon - Paris (gares dont l'id commence par « p_ »)
BBOX_FR = (41.0, 51.5, -5.5, 10.0)  # LGV et grandes lignes (gares dont l'id commence par « g_ » ou « m_ »)
EXT_PREFIX = "p_"
FR_PREFIXES = ("g_", "m_")
MATCH_LOW = 0.6                     # 2e passe : gares restantes, ressemblance plus faible (listées « à vérifier »)
MATCH_MIN = 0.78                    # seuil de ressemblance de nom (0.82 avant : trop strict pour les noms composés)

# Si une gare n'est pas reconnue automatiquement, force le nom GTFS exact ici.
# Exemple : {"mar": "Marseille Saint-Charles"}
OVERRIDES = {}

WEEK = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
STOPWORDS = {"gare", "de", "du", "des", "la", "le", "les", "l", "d", "sur", "en", "lez", "et", "a", "aux"}


def unquote(s):
    return s[1:-1].replace("\\'", "'").replace('\\"', '"')


def stations_from_html(path):
    """Lit les gares (id -> nom) déclarées dans index.html : appels S('id','Nom',...) et listes row([[id, nom], ...], ...)."""
    src = open(path, encoding="utf-8").read()
    q = r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    out = {}
    for m in re.finditer(r"\bS\(\s*'(\w+)'\s*,\s*" + q, src):
        out[m[1]] = unquote(m[2])
    for blk in re.finditer(r"\brow\(\s*\[(.*?)\]\s*,\s*-?\d", src, re.S):
        for m in re.finditer(r"\[\s*'(\w+)'\s*,\s*" + q, blk[1]):
            out.setdefault(m[1], unquote(m[2]))
    return out


def coords_from_html(path):
    """Coordonnées GPS de secours déclarées dans index.html : const FB={id:[lat,lon],...}."""
    src = open(path, encoding="utf-8").read()
    m = re.search(r"\bFB\s*=\s*\{(.*?)\}\s*;", src, re.S)
    out = {}
    if m:
        for k in re.finditer(r"(\w+)\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]", m[1]):
            out[k[1]] = (float(k[2]), float(k[3]))
    return out


def km(a, b):
    """Distance approximative (km) entre deux points (lat, lon)."""
    la = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 111.2 * math.cos(la))


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
    if len(small) == 1 and len(big) <= 4 and next(iter(small)) == ka.split()[0] == kb.split()[0]:
        return 0.83  # « Chambéry » ~ « Chambéry Challes les Eaux » : premier mot identique, à vérifier
    common = len(small & big)
    if common >= 2 and common / len(small) >= 0.66:
        return 0.86
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
        last = None
        for essai in range(1, 5):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                print(f"  {len(data) / 1e6:.1f} Mo reçus", file=sys.stderr)
                return zipfile.ZipFile(io.BytesIO(data))
            except Exception as e:  # réseau, zip tronqué…
                last = e
                print(f"  essai {essai}/4 échoué : {e}", file=sys.stderr)
                time.sleep(10 * essai)
        sys.exit(f"ERREUR : impossible de télécharger le GTFS ({last}). Télécharge le zip toi-même et lance : python build_horaires.py --gtfs chemin/du/fichier.zip")
    return zipfile.ZipFile(src)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=GTFS_URL, help="URL ou chemin du zip GTFS")
    ap.add_argument("--html", default="index.html")
    ap.add_argument("--out", default="horaires-paca.json")
    ap.add_argument("--days", type=int, default=14, help="jours couverts (30 max)")
    ap.add_argument("--ter-only", action="store_true", help="horaires-paca.json : ne garder que les TER (par défaut : TER, Intercités, TGV)")
    ap.add_argument("--all-modes", action="store_true", help=argparse.SUPPRESS)  # ancien nom, sans effet : c'est le défaut
    ap.add_argument("--france", default="horaires-france.json", help="fichier des horaires de toute la France pour la recherche de trajet (vide = désactivé)")
    ap.add_argument("--france-days", type=int, default=7, help="jours couverts par le fichier France")
    ap.add_argument("--stops-out", default="stops-france.json", help="table id d'arrêt -> gare (pour les incidents)")
    a = ap.parse_args()
    days = max(1, min(30, a.days))

    stations = stations_from_html(a.html)
    print(f"{len(stations)} gares lues dans {a.html}", file=sys.stderr)
    zf = open_gtfs(a.gtfs)

    # 1) Gares GTFS dans la zone, regroupées par nom normalisé
    stop_name, by_key = {}, {"paca": {}, "ext": {}, "fr": {}}
    for r in read_rows(zf, "stops.txt"):
        stop_name[r["stop_id"]] = r["stop_name"]
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except (ValueError, KeyError):
            continue
        if r.get("location_type", "") not in ("", "0", "1"):
            continue
        for zone, box in (("paca", BBOX), ("ext", BBOX_EXT), ("fr", BBOX_FR)):
            if box[0] <= lat <= box[1] and box[2] <= lon <= box[3]:
                g = by_key[zone].setdefault(key(r["stop_name"]), {"name": r["stop_name"], "ids": set(), "pos": (lat, lon)})
                g["ids"].add(r["stop_id"])

    # 2) Rapprochement gare du plan <-> gare GTFS (nom, puis position GPS de secours)
    gps = coords_from_html(a.html)
    stop_to_map, matched, weak, missing, coords = {}, {}, [], [], {}
    retry = []

    def register(sid, main, sc, how, zone):
        matched[sid] = main["name"]
        coords[sid] = [round(main["pos"][0], 5), round(main["pos"][1], 5)]
        ids = set(main["ids"])
        # variantes de la même gare à moins de 800 m (« Paris Gare de Lyon Hall 1 - 2 », « … Hall 3 »…)
        mt = set(key(main["name"]).split())
        for g in zone.values():
            if g is not main and km(g["pos"], main["pos"]) <= 0.8 and mt and mt <= set(key(g["name"]).split()):
                ids |= g["ids"]
        for stop_id in ids:
            stop_to_map.setdefault(stop_id, [])
            if sid not in stop_to_map[stop_id]:
                stop_to_map[stop_id].append(sid)
        if sc < 0.95 or how != "nom":
            weak.append(f"{sid}: « {stations[sid]} » -> « {main['name']} » ({how}, {sc:.2f})")

    for sid, name in stations.items():
        zone = by_key["ext" if sid.startswith(EXT_PREFIX) else "fr" if sid.startswith(FR_PREFIXES) else "paca"]
        is_g = sid.startswith(FR_PREFIXES)
        parts = [p for p in name.split(" - ") if p.strip()]
        # nom complet et premier élément : poids plein ; éléments suivants : pénalisés (évite « Six-Fours » -> La Seyne)
        variants = [(name, 1.0)] + [(p, 1.0 if i == 0 else 0.85) for i, p in enumerate(parts)]
        if is_g:  # « Le Creusot TGV » : la gare GTFS s'appelle « Le Creusot Montceau Montchanin »
            variants += [(re.sub(r"\bTGV\b", "", v, flags=re.I).strip(), 0.9 * w) for v, w in variants]
        best, how = (0.0, None), "nom"
        if sid in OVERRIDES:
            best, how = (1.0, zone.get(key(OVERRIDES[sid]))), "override"
        else:
            for g in zone.values():
                sc = max(score(v, g["name"]) * w for v, w in variants)
                if sc > best[0]:
                    best = (sc, g)
        if (best[1] is None or best[0] < MATCH_MIN) and sid in gps:
            # repli : la gare GTFS la plus proche de la position connue, si le nom ressemble un peu
            near_g = []
            for g in zone.values():
                d = km(gps[sid], g["pos"])
                sc = max(score(v, g["name"]) * w for v, w in variants)
                if (d <= 2.0 and sc >= 0.35) or (d <= 8.0 and sc >= 0.5) or (is_g and d <= 4.0):
                    near_g.append((d - 3.0 * sc, g, sc))
            if near_g:
                near_g.sort(key=lambda x: x[0])
                best, how = (max(0.8, near_g[0][2]), near_g[0][1]), "GPS"
        if best[1] is None or best[0] < MATCH_MIN:
            retry.append((sid, name, zone, variants))
            continue
        register(sid, best[1], best[0], how, zone)

    # 2 bis) Deuxième chance : ressemblance plus faible, mais uniquement avec des gares GTFS pas encore prises
    used = {g for g in matched.values()}
    for sid, name, zone, variants in retry:
        cands = [(max(score(v, g["name"]) * w for v, w in variants), g) for g in zone.values() if g["name"] not in used]
        cands.sort(key=lambda x: -x[0])
        if cands and cands[0][0] >= MATCH_LOW:
            used.add(cands[0][1]["name"])
            register(sid, cands[0][1], cands[0][0], "approx", zone)
        else:
            near = [g["name"] for g in sorted(zone.values(), key=lambda g: -max(score(v, g["name"]) for v in [name] + name.split(" - ")))[:3]]
            missing.append(f"{sid} ({name})  ->  noms proches : " + " | ".join(near))

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
        if re.search(r"EUROSTAR|THALYS", txt, re.I): return "EUROSTAR"
        if re.search(r"TRENITALIA|FRECCIA", txt, re.I): return "TRENITALIA"
        if re.search(r"RENFE|\bAVE\b", txt, re.I): return "RENFE"
        if re.search(r"LYRIA", txt, re.I): return "LYRIA"
        if re.search(r"INOUI", txt, re.I): return "INOUI"
        if re.search(r"\bTGV\b", txt, re.I): return "INOUI"   # TGV générique -> inOui
        if re.search(r"INTERCIT", txt, re.I): return "INTERCITÉS"
        if re.search(r"\bTER\b", txt, re.I): return "TER"
        return ""
    agencies = {r.get("agency_id", ""): r.get("agency_name", "") for r in read_rows(zf, "agency.txt")} if "agency.txt" in zf.namelist() else {}
    routes = {}
    for r in read_rows(zf, "routes.txt"):
        txt = " ".join(r.get(c, "") for c in ("route_id", "route_short_name", "route_long_name", "route_desc"))
        txt += " " + agencies.get(r.get("agency_id", ""), "")
        routes[r["route_id"]] = (bool(re.search(r"\bTER\b", txt, re.I)), kind(txt))
    a.all_modes = not a.ter_only
    if a.ter_only:
        kept = sum(1 for v in routes.values() if v[0])
        print(f"{kept}/{len(routes)} routes reconnues comme TER", file=sys.stderr)
        if kept == 0:
            print("Aucune route TER reconnue : repli sur toutes les routes (TGV/IC inclus).", file=sys.stderr)
            a.all_modes = True

    trips, all_trips = {}, {}
    for r in read_rows(zf, "trips.txt"):
        if not svc.get(r["service_id"], 0):
            continue
        ister, typ = routes.get(r["route_id"], (False, ""))
        num = (r.get("trip_short_name") or r.get("trip_headsign") or "").strip()
        if not typ:
            typ = kind(r["trip_id"] + " " + r.get("trip_headsign", ""))
        rec = (r["service_id"], num, typ, r["route_id"])
        all_trips[r["trip_id"]] = rec
        if a.all_modes or ister:
            trips[r["trip_id"]] = rec

    # 5) Arrêts de chaque voyage (on garde tout : premier, dernier et gares du plan)
    rows = {}
    for r in read_rows(zf, "stop_times.txt"):
        tid = r["trip_id"]
        if tid not in (all_trips if a.france else trips):
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
        if tid not in trips:
            continue
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
    vides = [f"{sid} ({stations[sid]})" for sid, v in result["stations"].items() if not v["d"] and not v["a"]]
    if vides:
        print(f"{len(vides)} gare(s) rapprochées mais SANS horaire sur la période (halte fermée ou non desservie) :\n  " + "\n  ".join(vides), file=sys.stderr)

    # 6) Horaires de toute la France (recherche de trajet locale) : format compact
    if a.france:
        fd = max(1, min(days, a.france_days))
        fmask = (1 << fd) - 1
        names, idx_by_key, sidx = [], {}, {}

        def sindex(stop_id):
            i = sidx.get(stop_id)
            if i is None:
                nm = stop_name.get(stop_id, stop_id)
                k = key(nm)
                i = idx_by_key.get(k)
                if i is None:
                    i = len(names)
                    names.append(nm)
                    idx_by_key[k] = i
                sidx[stop_id] = i
            return i

        types, tix, ftrips = [], {}, []
        for tid, rs in rows.items():
            service, num, typ, route_id = all_trips[tid]
            m = svc[service] & fmask
            if not m:
                continue
            rs.sort()
            if not typ:
                typ = kind(rs[0][1]) or kind(route_id)
            if not typ and len(re.sub(r"\D", "", num)) >= 5:
                typ = "TER"
            if typ not in tix:
                tix[typ] = len(types)
                types.append(typ)
            flat = [num, tix[typ], m]
            for _seq, stop_id, arr, dep, pu, do in rs:
                ta, td = minutes(arr), minutes(dep)
                ta = ta if (do != "1" and ta is not None) else -1   # descente possible
                td = td if (pu != "1" and td is not None) else -1   # montée possible
                if ta < 0 and td < 0:
                    continue
                flat += [sindex(stop_id), ta, td]
            if len(flat) >= 9:
                ftrips.append(flat)
        plan = {}
        for sid, gname in matched.items():
            i = idx_by_key.get(key(gname))
            if i is not None:
                plan[sid] = i
        fr = {"v": 3, "generated": start.isoformat(), "start": start.isoformat(), "days": fd,
              "types": types, "stops": names, "plan": plan, "trips": ftrips}
        with open(a.france, "w", encoding="utf-8") as f:
            json.dump(fr, f, ensure_ascii=False, separators=(",", ":"))
        if a.stops_out:
            with open(a.stops_out, "w", encoding="utf-8") as f:
                json.dump({"sid": sidx}, f, ensure_ascii=False, separators=(",", ":"))
        import os
        print(f"France : {len(ftrips)} trains, {len(names)} gares -> {a.france} ({os.path.getsize(a.france) / 1e6:.1f} Mo)", file=sys.stderr)
    print("Types de trains : " + ", ".join(f"{k} {v}" for k, v in sorted(tcount.items(), key=lambda kv: -kv[1])), file=sys.stderr)
    if unknown:
        print("Exemples de trains au type non reconnu :\n  " + "\n  ".join(unknown), file=sys.stderr)
    if weak:
        print("\nRapprochements à vérifier :\n  " + "\n  ".join(weak), file=sys.stderr)
    if missing:
        print("\nGares sans correspondance : recopie le bon nom dans OVERRIDES = {'id': 'Nom GTFS exact'} :\n  " + "\n  ".join(missing), file=sys.stderr)


if __name__ == "__main__":
    main()
