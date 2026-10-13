#!/usr/bin/env python3
"""
Génère incidents.json : perturbations en cours sur le réseau SNCF (TGV, Intercités, TER).

Sources (transport.data.gouv.fr, licence ouverte) :
  - alertes de service  : https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts
  - mises à jour de trains (suppressions, retards) : https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates

Aucune dépendance : le format GTFS-RT (protobuf) est décodé à la main.
À lancer APRÈS build_horaires.py (il lit horaires-paca.json pour relier les arrêts SNCF aux gares du plan).

Usage :
    python build_incidents.py                      # écrit incidents.json
    python build_incidents.py --alerts fichier.pb  # test hors ligne
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.request

ALERTS_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"
UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

MIN_DELAY = 15      # minutes : en dessous, un retard n'est pas listé
MAX_ITEMS = 400     # garde-fou : nombre maximum d'incidents listés
MAX_CANCELLED = 6000

# Effets GTFS-RT -> libellé affiché dans le panneau « Incidents » et gravité
EFFECTS = {1: ("Supprimé", "hi"), 2: ("Perturbation", "hi"), 3: ("Retard", "lo"), 4: ("Dévié", "lo"),
           5: ("Renfort", "lo"), 6: ("Modifié", "lo"), 7: ("Info", "lo"), 8: ("Info", "lo"),
           9: ("Arrêt déplacé", "lo"), 10: ("Info", "lo"), 11: ("Accessibilité", "lo")}


# ---------- décodage protobuf minimal ----------
def varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7


def fields(b):
    """Découpe un message protobuf en {numéro: [valeurs]} (entiers ou octets)."""
    out, i, n = {}, 0, len(b)
    while i < n:
        tag, i = varint(b, i)
        f, w = tag >> 3, tag & 7
        if w == 0:
            v, i = varint(b, i)
        elif w == 2:
            ln, i = varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        else:
            raise ValueError("type protobuf inconnu")
        out.setdefault(f, []).append(v)
    return out


def sint(v):
    """Entier protobuf signé (int32 stocké sur 64 bits)."""
    return v - (1 << 64) if v >= 1 << 63 else v


def text(ts):
    """TranslatedString -> texte (français de préférence)."""
    best = ""
    for tr in fields(ts).get(1, []):
        f = fields(tr)
        t = (f.get(1) or [b""])[0].decode("utf-8", "replace")
        lang = (f.get(2) or [b""])[0].decode("ascii", "ignore")
        if t and (not best or lang.startswith("fr")):
            best = t
    return best


def train_number(trip_id):
    """Numéro de train extrait d'un trip_id SNCF (ex. « OCESN861234F4100:2026-10-12 » -> 861234)."""
    m = re.search(r"OCE[A-Za-z]*?(\d{3,6})(?=[A-Z]|\b|:)", trip_id or "")
    return m[1] if m else ""


# ---------- accès réseau ----------
def fetch(src):
    if not re.match(r"https?://", src):
        return open(src, "rb").read()
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (compatible; carte-ter)"})
    last = None
    for essai in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            last = e
            print(f"  essai {essai}/3 échoué ({src.rsplit('/', 1)[-1]}) : {e}", file=sys.stderr)
            time.sleep(5 * essai)
    raise RuntimeError(last)


def load_stopmap(path):
    try:
        return json.load(open(path, encoding="utf-8")).get("stopmap", {})
    except Exception as e:
        print(f"  {path} illisible ({e}) : incidents sans lien vers les gares du plan.", file=sys.stderr)
        return {}


# ---------- construction ----------
def alerts(raw, stopmap):
    items, now = [], int(time.time())
    for ent in fields(raw).get(2, []):
        al = fields(ent).get(5)
        if not al:
            continue
        a = fields(al[0])
        periods = [fields(p) for p in a.get(1, [])]
        if periods and not any((int((p.get(1) or [0])[0]) or 0) <= now and (not p.get(2) or int(p[2][0]) >= now) for p in periods):
            continue  # alerte pas encore commencée ou terminée
        stations, trains = [], []
        for ie in a.get(5, []):
            e = fields(ie)
            for sid in (e.get(5) or []):
                for g in stopmap.get(sid.decode(), []):
                    if g not in stations:
                        stations.append(g)
            for t in (e.get(4) or []):
                n = train_number((fields(t).get(1) or [b""])[0].decode("utf-8", "replace"))
                if n and n not in trains:
                    trains.append(n)
        eff, sev = EFFECTS.get(int((a.get(7) or [8])[0]), ("Info", "lo"))
        header = text((a.get(10) or [b""])[0]) if a.get(10) else ""
        body = text((a.get(11) or [b""])[0]) if a.get(11) else ""
        if not (header or body):
            continue
        items.append({"header": header, "text": body, "effect": eff, "sev": sev, "stations": stations, "trains": trains})
    return items


def updates(raw, stopmap):
    """Trains supprimés (toute la France) + gros retards touchant le plan."""
    cancelled, items, samples = [], [], []
    for ent in fields(raw).get(2, []):
        tu = fields(ent).get(3)
        if not tu:
            continue
        t = fields(tu[0])
        td = fields((t.get(1) or [b""])[0])
        tid = (td.get(1) or [b""])[0].decode("utf-8", "replace")
        num = train_number(tid)
        if len(samples) < 5:
            samples.append(f"{tid} -> {num or '?'}")
        rel = int((td.get(4) or [0])[0])
        stations, maxd = [], 0
        for stu in t.get(2, []):
            s = fields(stu)
            for k in (2, 3):
                if s.get(k):
                    d = sint(int((fields(s[k][0]).get(1) or [0])[0]))
                    maxd = max(maxd, d)
            sid = (s.get(4) or [b""])[0].decode("utf-8", "replace")
            for g in stopmap.get(sid, []):
                if g not in stations:
                    stations.append(g)
        if not num:
            continue
        if rel == 3:   # CANCELED
            if num not in cancelled:
                cancelled.append(num)
            if stations:
                items.append({"header": f"Train {num} supprimé", "text": "", "effect": "Supprimé", "sev": "hi", "stations": stations, "trains": [num]})
        elif maxd >= MIN_DELAY * 60 and stations:
            items.append({"header": f"Train {num} : retard de {maxd // 60} min", "text": "", "effect": "Retard", "sev": "lo", "stations": stations, "trains": [num]})
    return cancelled, items, samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alerts", default=ALERTS_URL, help="URL ou fichier .pb des alertes")
    ap.add_argument("--updates", default=UPDATES_URL, help="URL ou fichier .pb des mises à jour de trains (vide = ignorer)")
    ap.add_argument("--horaires", default="horaires-paca.json", help="fichier produit par build_horaires.py")
    ap.add_argument("--out", default="incidents.json")
    a = ap.parse_args()

    stopmap = load_stopmap(a.horaires)
    items, cancelled = [], []
    try:
        items = alerts(fetch(a.alerts), stopmap)
        print(f"{len(items)} alertes en cours", file=sys.stderr)
    except Exception as e:
        sys.exit(f"ERREUR : alertes illisibles ({e})")
    if a.updates:
        try:
            cancelled, extra, samples = updates(fetch(a.updates), stopmap)
            print(f"{len(cancelled)} trains supprimés, {len(extra)} incidents de trains sur le plan", file=sys.stderr)
            print("  exemples de numéros extraits : " + " | ".join(samples), file=sys.stderr)
            items += extra
        except Exception as e:  # les mises à jour sont un plus : on garde les alertes
            print(f"Mises à jour de trains ignorées : {e}", file=sys.stderr)

    # sans doublon, graves d'abord, plafonné
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: x["sev"] != "hi"):
        k = (it["header"], it["effect"], tuple(it["trains"]))
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    out = {"generated": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
           "items": uniq[:MAX_ITEMS], "cancelled": cancelled[:MAX_CANCELLED]}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out['items'])} incidents écrits dans {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
