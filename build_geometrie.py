#!/usr/bin/env python3
"""
Génère geom-lignes.json : le tracé RÉEL des voies ferrées (OpenStreetMap) entre deux gares consécutives du plan.
La vue « Relief / Satellite » du site s'en sert pour que les lignes ET les trains suivent les vraies voies
(sans ce fichier, le site relie simplement les gares en ligne droite).

Entrée  : geom-paires.json (gares + paires de gares consécutives, fourni avec le site)
Sortie  : geom-lignes.json
Source  : OpenStreetMap via l'API Overpass (© contributeurs OpenStreetMap, licence ODbL)

Aucune dépendance. À lancer depuis GitHub Actions (accès Internet complet), environ 10 à 30 minutes.

Usage :
    python build_geometrie.py                      # télécharge les voies et écrit geom-lignes.json
    python build_geometrie.py --cache tuiles       # garde les téléchargements (reprise possible)
    python build_geometrie.py --offline tuiles     # n'utilise que le cache (tests)
"""
import argparse
import heapq
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import datetime as dt

MIRRORS = ["https://overpass-api.de/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter",
           "https://overpass.private.coffee/api/interpreter"]
TILE = 1.0          # degrés : taille d'une requête
BUF = 0.25          # degrés : marge autour de chaque paire de gares
SNAP_M = 2500       # m : distance max gare -> voie
QUERY = ('[out:json][timeout:300];way["railway"="rail"][!"service"]'
         '["usage"!~"^(industrial|military|tourism|test)$"](%.4f,%.4f,%.4f,%.4f);out geom qt;')
R = 6371000.0


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ---------- téléchargement ----------
def tile_name(i, j):
    return f"{i}_{j}.json"


def fetch_tile(i, j, cache, offline):
    path = os.path.join(cache, tile_name(i, j)) if cache else None
    if path and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    if offline:
        return None
    q = QUERY % (i * TILE - .01, j * TILE - .01, (i + 1) * TILE + .01, (j + 1) * TILE + .01)
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for essai in range(6):
        url = MIRRORS[essai % len(MIRRORS)]
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "carte-ter-geometrie/1.0 (GitHub Actions)"})
            with urllib.request.urlopen(req, timeout=400) as r:
                raw = r.read()
            js = json.loads(raw)
            if "elements" not in js:
                raise ValueError("réponse sans éléments")
            if path:
                os.makedirs(cache, exist_ok=True)
                json.dump(js, open(path, "w", encoding="utf-8"), separators=(",", ":"))
            return js
        except Exception as e:
            last = e
            print(f"  tuile {i},{j} essai {essai + 1}/6 ({url.split('/')[2]}) : {e}", file=sys.stderr)
            time.sleep(10 * (essai + 1))
    print(f"  tuile {i},{j} abandonnée : {last}", file=sys.stderr)
    return None


# ---------- graphe ferroviaire ----------
class Graph:
    def __init__(self):
        self.id = {}
        self.xy = []
        self.adj = []
        self.grid = {}

    def node(self, lat, lon):
        k = (round(lat, 6), round(lon, 6))
        n = self.id.get(k)
        if n is None:
            n = len(self.xy)
            self.id[k] = n
            self.xy.append(k)
            self.adj.append([])
            self.grid.setdefault((int(k[0] / .02), int(k[1] / .02)), []).append(n)
        return n

    def add_way(self, geom, tags):
        hs = tags.get("highspeed") == "yes"
        try:
            if float(str(tags.get("maxspeed", "0")).split()[0]) >= 200:
                hs = True
        except ValueError:
            pass
        u = tags.get("usage", "")
        f = 1.0 if u == "main" else 1.05 if u == "branch" else 1.3
        prev = None
        for p in geom:
            n = self.node(p["lat"], p["lon"])
            if prev is not None and prev != n:
                d = hav(self.xy[prev], self.xy[n])
                self.adj[prev].append((n, d, f, hs))
                self.adj[n].append((prev, d, f, hs))
            prev = n

    def snap(self, c):
        ci, cj = int(c[0] / .02), int(c[1] / .02)
        best, bd = None, SNAP_M
        for di in range(-2, 3):
            for dj in range(-2, 3):
                for n in self.grid.get((ci + di, cj + dj), ()):
                    if not self.adj[n]:
                        continue
                    d = hav(c, self.xy[n])
                    if d < bd:
                        best, bd = n, d
        return best

    def route(self, a, b, flavor):
        """A* : 'h' = voies à grande vitesse favorisées, 'c' = lignes classiques favorisées."""
        if a == b:
            return [a]
        hf, cf = (0.6, 2.5) if flavor == "h" else (6.0, 1.0)   # (facteur voie LV, facteur voie classique)
        direct = hav(self.xy[a], self.xy[b])
        limit = (direct * 3 + 30000) * 1.3
        hmin = 0.55   # minorant du facteur de coût (0,6 voie LV / 1,0 classique)
        goal = self.xy[b]
        g = {a: 0.0}
        prev = {}
        pq = [(hmin * direct, a)]
        done = set()
        while pq:
            _, u = heapq.heappop(pq)
            if u in done:
                continue
            if u == b:
                out = [b]
                while out[-1] != a:
                    out.append(prev[out[-1]])
                return out[::-1]
            done.add(u)
            gu = g[u]
            for v, d, f, hs in self.adj[u]:
                w = d * f * (hf if hs else cf)
                ng = gu + w
                if ng > limit or ng >= g.get(v, 1e18):
                    continue
                g[v] = ng
                prev[v] = u
                heapq.heappush(pq, (ng + hmin * hav(self.xy[v], goal), v))
        return None


def dp(pts, tol):
    """Douglas-Peucker (pts = [(lat,lon)])."""
    if len(pts) < 3:
        return pts
    k = math.cos(math.radians(pts[len(pts) // 2][0]))
    P = [((p[1]) * 111320 * k, p[0] * 110540) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    st = [(0, len(pts) - 1)]
    while st:
        i, j = st.pop()
        ax, ay = P[i]
        bx, by = P[j]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        md, mi = -1, -1
        for m in range(i + 1, j):
            d = abs(dy * (P[m][0] - ax) - dx * (P[m][1] - ay)) / L
            if d > md:
                md, mi = d, m
        if md > tol:
            keep[mi] = True
            st.append((i, mi))
            st.append((mi, j))
    return [p for p, kp in zip(pts, keep) if kp]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paires", default="geom-paires.json")
    ap.add_argument("--out", default="geom-lignes.json")
    ap.add_argument("--cache", default="")
    ap.add_argument("--offline", default="", help="dossier de tuiles déjà téléchargées (aucun accès réseau)")
    ap.add_argument("--tol", type=float, default=12.0, help="simplification des tracés (mètres)")
    a = ap.parse_args()
    cache = a.offline or a.cache
    P = json.load(open(a.paires, encoding="utf-8"))
    ST, pairs = P["stations"], P["pairs"]

    # tuiles nécessaires
    tiles = set()
    for x, y, f in pairs:
        if x not in ST or y not in ST:
            continue
        la0 = min(ST[x][0], ST[y][0]) - BUF
        la1 = max(ST[x][0], ST[y][0]) + BUF
        lo0 = min(ST[x][1], ST[y][1]) - BUF
        lo1 = max(ST[x][1], ST[y][1]) + BUF
        for i in range(math.floor(la0 / TILE), math.floor(la1 / TILE) + 1):
            for j in range(math.floor(lo0 / TILE), math.floor(lo1 / TILE) + 1):
                tiles.add((i, j))
    print(f"{len(pairs)} paires de gares, {len(tiles)} tuiles à lire", file=sys.stderr)

    G = Graph()
    ok_tiles = 0
    for n, (i, j) in enumerate(sorted(tiles), 1):
        js = fetch_tile(i, j, cache, bool(a.offline))
        if not js:
            continue
        ok_tiles += 1
        for el in js.get("elements", []):
            if el.get("type") == "way" and el.get("geometry"):
                G.add_way(el["geometry"], el.get("tags", {}))
        if not a.offline:
            print(f"  tuile {n}/{len(tiles)} : {len(G.xy)} points de voie", file=sys.stderr)
            time.sleep(2)
    if not ok_tiles:
        sys.exit("ERREUR : aucune tuile OpenStreetMap lisible (Overpass indisponible ?). Ancien fichier conservé.")

    snapped = {}
    for sid, c in ST.items():
        snapped[sid] = G.snap(c)
    out, fail = {}, []
    for x, y, f in pairs:
        # clé triée : a|b|f ; tracé stocké dans le sens a -> b
        key = f"{x}|{y}|{f}"
        if x not in ST or y not in ST or snapped.get(x) is None or snapped.get(y) is None:
            fail.append(key)
            continue
        path = G.route(snapped[x], snapped[y], f)
        if not path:
            fail.append(key)
            continue
        pts = [G.xy[n] for n in path]
        pts = dp(pts, a.tol)
        # raccord exact avec les coordonnées des gares du plan (marqueur sur la voie)
        pts = [tuple(ST[x])] + pts + [tuple(ST[y])]
        out[key] = [[round(p[0], 5), round(p[1], 5)] for p in pts]
    print(f"{len(out)} tracés calculés, {len(fail)} sans tracé (le site relie alors les gares en ligne droite)", file=sys.stderr)
    if fail:
        print("  sans tracé : " + ", ".join(fail[:40]) + (" …" if len(fail) > 40 else ""), file=sys.stderr)
    if len(out) < len(pairs) * 0.3:
        sys.exit("ERREUR : trop peu de tracés calculés, fichier non écrit (l'ancien est conservé).")
    res = {"v": 1, "generated": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
           "source": "© contributeurs OpenStreetMap (ODbL)", "e": out}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, separators=(",", ":"))
    print(f"{a.out} écrit ({os.path.getsize(a.out) // 1024} ko)", file=sys.stderr)


if __name__ == "__main__":
    main()
