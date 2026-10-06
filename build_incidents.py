#!/usr/bin/env python3
"""
Génère incidents.json : les alertes temps réel SNCF (GTFS-RT « Service Alerts »)
qui concernent les gares du plan ou des trains présents dans horaires-paca.json.

Prérequis : horaires-paca.json déjà généré par build_horaires.py, et
  pip install gtfs-realtime-bindings

Usage : python3 build_incidents.py [--url URL] [--horaires horaires-paca.json] [--out incidents.json]
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.request

from google.transit import gtfs_realtime_pb2

URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"

# Effect GTFS-RT -> (libellé affiché, gravité)
EFFECTS = {
    1: ("Supprimé", "hi"), 2: ("Service réduit", "lo"), 3: ("Retards", "lo"), 4: ("Déviation", "lo"),
    5: ("Service ajouté", "lo"), 6: ("Modifié", "lo"), 7: ("Perturbation", "lo"), 8: ("Perturbation", "lo"),
    9: ("Arrêt déplacé", "lo"), 10: ("Information", "lo"), 11: ("Accessibilité", "lo"),
}


def text(ts):
    """Texte traduit : français si dispo, sinon le premier."""
    if not ts.translation:
        return ""
    for t in ts.translation:
        if t.language.lower().startswith("fr"):
            return t.text.strip()
    return ts.translation[0].text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--horaires", default="horaires-paca.json")
    ap.add_argument("--out", default="incidents.json")
    a = ap.parse_args()

    try:
        with open(a.horaires, encoding="utf-8") as f:
            hor = json.load(f)
    except FileNotFoundError:
        sys.exit("horaires-paca.json introuvable : lance d'abord le workflow « Mise à jour des horaires ».")

    stopmap = hor.get("stopmap", {})
    known = set()
    for st in hor["stations"].values():
        for k in ("d", "a"):
            known.update(e[2] for e in st[k] if e[2])

    req = urllib.request.Request(a.url, headers={"User-Agent": "Mozilla/5.0 (compatible; carte-ter)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)

    now = time.time()
    items, total = [], 0
    for ent in feed.entity:
        if not ent.HasField("alert"):
            continue
        total += 1
        al = ent.alert
        # période d'activité : en cours, ou qui démarre dans les 24 h
        if al.active_period:
            ok = any((p.start == 0 or p.start <= now + 86400) and (p.end == 0 or p.end >= now) for p in al.active_period)
            if not ok:
                continue
        header, desc = text(al.header_text), text(al.description_text)
        stations, trains = [], []
        for ie in al.informed_entity:
            if ie.stop_id:
                for sid in stopmap.get(ie.stop_id, []):
                    if sid not in stations:
                        stations.append(sid)
            for ident in (ie.trip.trip_id, ie.route_id):
                for num in re.findall(r"\d{3,6}", ident or ""):
                    if num in known and num not in trains:
                        trains.append(num)
        for num in re.findall(r"(?:train|TGV|TER|n°)\s*(?:n°\s*)?(\d{3,6})", header + " " + desc, re.I):
            if num in known and num not in trains:
                trains.append(num)
        if not stations and not trains:
            continue
        label, sev = EFFECTS.get(int(al.effect), ("Perturbation", "lo"))
        items.append({
            "header": header or label, "text": desc[:400], "effect": label, "sev": sev,
            "stations": stations[:12], "trains": trains[:12],
        })

    items.sort(key=lambda i: 0 if i["sev"] == "hi" else 1)
    out = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "total": total, "items": items[:80]}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{total} alertes lues, {len(items)} concernent le plan -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
