#!/usr/bin/env python3
"""
Génère geom-lignes.json : le tracé RÉEL des voies ferrées (OpenStreetMap) entre deux gares consécutives du plan.
La vue « Relief / Satellite » du site s'en sert pour que les lignes ET les trains suivent les vraies voies
(sans ce fichier, le site relie simplement les gares en ligne droite).

Entrée  : geom-paires.json (gares + paires de gares consécutives, fourni avec le site)
          geom-corrections.json (facultatif, voir plus bas)
Sortie  : geom-lignes.json
Source  : OpenStreetMap via l'API Overpass (© contributeurs OpenStreetMap, licence ODbL)

Aucune dépendance. À lancer depuis GitHub Actions (accès Internet complet).

Version « haute précision » :
  - aucune tuile ne peut manquer (sinon le script s'arrête, les tuiles déjà reçues restent en cache) ;
  - les réponses Overpass tronquées (erreur serveur) sont refusées et retentées ;
  - simplification des tracés à 2 m, coordonnées à 6 décimales (~0,1 m) ;
  - VOIES CANDIDATES : chaque gare peut s'accrocher à plusieurs voies distinctes proches (jusqu'à 4 dans 300 m) et le
    script garde la combinaison qui donne le meilleur trajet (évite de prendre la mauvaise voie) ;
  - TROUS OSM : si un trajet est impossible, les bouts de voie séparés de moins de 150 m sont reliés
    (uniquement autour de la paire concernée) puis le trajet est retenté ;
  - CORRECTIONS : fichier geom-corrections.json pour corriger une gare mal placée ou forcer la voie ;
  - le fichier n'est écrit que si au moins 95 % des tracés sont calculés.

geom-corrections.json (facultatif) :
    {
      "gares": { "luc": [43.3902379, 6.3430972] },   // remplace les coordonnées de la gare pour ce script
      "voies": { "xyz": [43.12345, 5.67890] }        // point exact SUR la bonne voie (la gare garde ses coordonnées)
    }
  Les clés qui commencent par « _ » sont ignorées (commentaires).
  Si tu corriges une gare dans « gares », corrige aussi ses coordonnées dans le plan du site.

Usage :
    python build_geometrie.py --cache .tuiles --max-minutes 100
    python build_geometrie.py --offline .tuiles    # n'utilise que le cache (tests)
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
TILE = 0.5          # degrés : taille d'une requête (plus petit = plus léger pour Overpass)
BUF = 0.25          # degrés : marge autour de chaque paire de gares
SNAP_M = 800        # m : distance max gare -> voie
SNAP_WARN_M = 200   # m : au-delà, la gare est signalée dans les logs
CAND_N = 4          # nombre max de voies candidates par gare
CAND_M = 300        # m : rayon de recherche des voies candidates supplémentaires
SNAP_PEN = 4.0      # coût d'un mètre hors voie (en mètres de voie) : la voie la plus proche reste favorite
GAP_M = 150         # m : trou OSM maximal comblé
BRIDGE_BUF = 0.05   # degrés : zone autour d'une paire où l'on cherche les trous
ESSAIS = 4          # essais par tuile
HTTP_TIMEOUT = 200  # s : durée max d'une requête
QUERY = ('[out:json][timeout:180];way["railway"="rail"][!"service"]'
         '["usage"!~"^(industrial|military|tourism|test)$"](%.4f,%.4f,%.4f,%.4f);out geom qt;')
R = 6371000.0


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ---------- téléchargement ----------
def tile_name(i, j):
    return f"{i}_{j}.json"


def cache_path(i, j, cache):
    return os.path.join(cache, tile_name(i, j)) if cache else None


def fetch_tile(i, j, cache, offline):
    path = cache_path(i, j, cache)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass  # fichier de cache abîmé : on retélécharge
    if offline:
        return None
    q = QUERY % (i * TILE - .01, j * TILE - .01, (i + 1) * TILE + .01, (j + 1) * TILE + .01)
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for essai in range(ESSAIS):
        url = MIRRORS[essai % len(MIRRORS)]
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "carte-ter-geometrie/3.0 (GitHub Actions)"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                raw = r.read()
            js = json.loads(raw)
            if "elements" not in js:
                raise ValueError("réponse sans éléments")
            # Overpass renvoie parfois un résultat PARTIEL avec une remarque d'erreur : on le refuse
            remark = str(js.get("remark", ""))
            if "error" in remark.lower() or "timed out" in remark.lower() or "out of memory" in remark.lower():
                raise ValueError("réponse incomplète : " + remark[:120])
            if path:
                os.makedirs(cache, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(js, fh, separators=(",", ":"))
                os.replace(tmp, path)  # écriture atomique : jamais de fichier à moitié écrit
            return js
        except Exception as e:
            last = e
            log(f"  tuile {i},{j} essai {essai + 1}/{ESSAIS} ({url.split('/')[2]}) : {e}")
            if essai < ESSAIS - 1:
                time.sleep(10 * (essai + 1))
    log(f"  tuile {i},{j} abandonnée : {last}")
    return None


# ---------- graphe ferroviaire ----------
class Graph:
    def __init__(self):
        self.id = {}
        self.xy = []
        self.adj = []
        self.grid = {}
        self._par = None   # composantes connexes (calculées seulement si un trou doit être comblé)

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

    def candidates(self, c, maxn=CAND_N):
        """Voies proches de c : {noeud: distance en m}. On garde le point le plus proche de chaque
        « voie » distincte (ensemble de rails reliés entre eux dans un rayon de CAND_M m), de la plus
        proche à la plus éloignée, jusqu'à maxn. La voie la plus proche (<= SNAP_M) est toujours retenue."""
        ci, cj = int(c[0] / .02), int(c[1] / .02)
        near = []
        for di in range(-2, 3):
            for dj in range(-2, 3):
                for n in self.grid.get((ci + di, cj + dj), ()):
                    if not self.adj[n]:
                        continue
                    d = hav(c, self.xy[n])
                    if d < SNAP_M:
                        near.append((d, n))
        if not near:
            return {}
        near.sort()
        keep = [x for x in near if x[0] <= CAND_M] or [near[0]]
        inset = {n for _, n in keep}
        seen, out = set(), []
        for d, n in keep:
            if n in seen:
                continue
            out.append((d, n))
            seen.add(n)
            stack = [n]
            while stack:   # marque toute la voie reliée à ce point
                u = stack.pop()
                for v, _, _, _ in self.adj[u]:
                    if v in inset and v not in seen:
                        seen.add(v)
                        stack.append(v)
            if len(out) >= maxn:
                break
        return {n: d for d, n in out}

    # -- composantes connexes et comblement des trous --
    def _find(self, x):
        p = self._par
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def _components(self):
        if self._par is not None:
            return
        self._par = list(range(len(self.xy)))
        for u in range(len(self.xy)):
            for v, _, _, _ in self.adj[u]:
                if v > u:
                    ru, rv = self._find(u), self._find(v)
                    if ru != rv:
                        self._par[ru] = rv

    def bridge_gaps(self, lat0, lat1, lon0, lon1):
        """Relie les bouts de voie (impasses) à la voie d'une AUTRE composante située à moins de GAP_M m,
        dans la zone donnée. Retourne le nombre de liaisons ajoutées."""
        self._components()
        fine, ends = {}, []
        for ci in range(int(lat0 / .02) - 1, int(lat1 / .02) + 2):
            for cj in range(int(lon0 / .02) - 1, int(lon1 / .02) + 2):
                for n in self.grid.get((ci, cj), ()):
                    la, lo = self.xy[n]
                    if lat0 <= la <= lat1 and lon0 <= lo <= lon1:
                        fine.setdefault((int(la / .003), int(lo / .003)), []).append(n)
                        if len({v for v, _, _, _ in self.adj[n]}) == 1:
                            ends.append(n)
        added = 0
        for e in ends:
            ce = self._find(e)
            la, lo = self.xy[e]
            ci, cj = int(la / .003), int(lo / .003)
            best, bd = None, GAP_M
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for m in fine.get((ci + di, cj + dj), ()):
                        if m == e or self._find(m) == ce:
                            continue
                        d = hav(self.xy[e], self.xy[m])
                        if d < bd:
                            best, bd = m, d
            if best is not None:
                self.adj[e].append((best, bd, 1.3, False))
                self.adj[best].append((e, bd, 1.3, False))
                ru, rv = self._find(e), self._find(best)
                self._par[ru] = rv
                added += 1
        return added

    def route(self, starts, goals, flavor):
        """A* entre deux ensembles de voies candidates ({noeud: distance gare->voie en m}).
        'h' = voies à grande vitesse favorisées, 'c' = lignes classiques favorisées."""
        if not starts or not goals:
            return None
        hf, cf = (0.6, 2.5) if flavor == "h" else (6.0, 1.0)   # (facteur voie LV, facteur voie classique)
        hmin = min(hf, cf)   # coût minimal par mètre (facteur d'usage >= 1) : heuristique exacte et admissible
        gxy = [self.xy[b] for b in goals]
        direct = min(hav(self.xy[a], q) for a in starts for q in gxy)
        # plafond de coût (en unités de coût, pas en mètres) : plus large en mode « h » car une voie
        # classique y coûte 2,5 fois sa longueur
        limit = (direct * 3 + 30000) * 1.3 * (cf if flavor == "h" else 1.0) + 4 * SNAP_PEN * SNAP_M

        def h(v):
            p = self.xy[v]
            return hmin * min(hav(p, q) for q in gxy)

        g, prev, pq, done = {}, {}, [], set()
        for a, d in starts.items():
            g[a] = d * SNAP_PEN
            pq.append((g[a] + h(a), a))
        heapq.heapify(pq)
        best = None   # (coût total, noeud d'arrivée)
        while pq:
            f, u = heapq.heappop(pq)
            if best is not None and f >= best[0]:
                break   # plus rien ne peut faire mieux
            if u in done:
                continue
            done.add(u)
            gu = g[u]
            if u in goals:
                tot = gu + goals[u] * SNAP_PEN
                if best is None or tot < best[0]:
                    best = (tot, u)
            for v, d, fct, hs in self.adj[u]:
                ng = gu + d * fct * (hf if hs else cf)
                if ng > limit or ng >= g.get(v, 1e18):
                    continue
                g[v] = ng
                prev[v] = u
                heapq.heappush(pq, (ng + h(v), v))
        if best is None:
            return None
        out = [best[1]]
        while out[-1] in prev:
            out.append(prev[out[-1]])
        return out[::-1]


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
    ap.add_argument("--corrections", default="geom-corrections.json")
    ap.add_argument("--cache", default="")
    ap.add_argument("--offline", default="", help="dossier de tuiles déjà téléchargées (aucun accès réseau)")
    ap.add_argument("--tol", type=float, default=2.0, help="simplification des tracés (mètres)")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="arrête les téléchargements après N minutes (les tuiles reçues restent en cache)")
    a = ap.parse_args()
    cache = a.offline or a.cache
    with open(a.paires, encoding="utf-8") as fh:
        P = json.load(fh)
    ST, pairs = P["stations"], P["pairs"]

    # corrections manuelles (facultatif)
    voies = {}
    if a.corrections and os.path.exists(a.corrections):
        with open(a.corrections, encoding="utf-8") as fh:
            corr = json.load(fh)
        for sid, c in corr.get("gares", {}).items():
            if sid.startswith("_"):
                continue
            if sid not in ST:
                log(f"  correction ignorée : gare inconnue « {sid} »")
                continue
            log(f"  correction : gare {sid} déplacée de {hav(ST[sid], c):.0f} m")
            ST[sid] = [c[0], c[1]]
        for sid, c in corr.get("voies", {}).items():
            if sid.startswith("_"):
                continue
            if sid not in ST:
                log(f"  correction ignorée : gare inconnue « {sid} »")
                continue
            voies[sid] = c
            log(f"  correction : gare {sid} accrochée au point de voie {c[0]}, {c[1]}")

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
    log(f"{len(pairs)} paires de gares, {len(tiles)} tuiles à lire")

    G = Graph()
    ok_tiles = 0
    missing = []
    seen_ways = set()
    t0 = time.time()
    deadline = t0 + a.max_minutes * 60 if a.max_minutes else None
    for n, (i, j) in enumerate(sorted(tiles), 1):
        p = cache_path(i, j, cache)
        cached = bool(p and os.path.exists(p))
        if deadline and not a.offline and not cached and time.time() > deadline:
            missing.append((i, j))   # temps écoulé : on ne télécharge plus, on lit seulement le cache
            continue
        js = fetch_tile(i, j, cache, bool(a.offline))
        if not js:
            missing.append((i, j))
            continue
        ok_tiles += 1
        for el in js.get("elements", []):
            if el.get("type") == "way" and el.get("geometry"):
                wid = el.get("id")
                if wid is not None:
                    if wid in seen_ways:
                        continue   # voie déjà lue dans une tuile voisine (zone de recouvrement)
                    seen_ways.add(wid)
                G.add_way(el["geometry"], el.get("tags", {}))
        if not a.offline:
            log(f"  tuile {n}/{len(tiles)} {'(cache)' if cached else '(téléchargée)'} : "
                f"{len(G.xy)} points de voie, {int((time.time() - t0) / 60)} min écoulées")
            if not cached:
                time.sleep(2)
    if missing:
        msg = (f"{len(missing)} tuile(s) manquante(s) sur {len(tiles)} : {missing[:15]}"
               f"{' …' if len(missing) > 15 else ''}")
        if a.offline:
            log("ATTENTION (mode hors ligne) : " + msg)
        else:
            sys.exit("ERREUR : " + msg + ". Aucun fichier écrit pour ne pas produire de tracés faux. "
                     "Relance le workflow : les tuiles déjà reçues sont en cache et ne seront pas retéléchargées.")
    if not ok_tiles:
        sys.exit("ERREUR : aucune tuile OpenStreetMap lisible (Overpass indisponible ?). Ancien fichier conservé.")

    # voies candidates de chaque gare
    snapped = {}
    for sid, c in ST.items():
        if sid in voies:
            cand = G.candidates(voies[sid], maxn=1)
            if not cand:
                log(f"  gare {sid} : aucune voie à moins de {SNAP_M} m du point de voie indiqué")
        else:
            cand = G.candidates(c)
            if not cand:
                log(f"  gare {sid} : aucune voie à moins de {SNAP_M} m")
            else:
                dmin = min(cand.values())
                if dmin > SNAP_WARN_M:
                    log(f"  gare {sid} à {dmin:.0f} m de la voie la plus proche (à vérifier)")
        snapped[sid] = cand

    def build(x, y, path):
        pts = [G.xy[nd] for nd in path]
        pts = dp(pts, a.tol)
        # raccord exact avec les coordonnées des gares du plan (marqueur sur la voie)
        pts = [tuple(ST[x])] + pts + [tuple(ST[y])]
        return [[round(p[0], 6), round(p[1], 6)] for p in pts]

    # 1re passe : tous les trajets
    res, fail, todo = {}, [], []
    for k, (x, y, f) in enumerate(pairs, 1):
        # clé : a|b|f ; tracé stocké dans le sens a -> b
        key = f"{x}|{y}|{f}"
        if x not in ST or y not in ST or not snapped.get(x) or not snapped.get(y):
            fail.append(key)
            continue
        path = G.route(snapped[x], snapped[y], f)
        if path:
            res[key] = build(x, y, path)
        else:
            todo.append((x, y, f, key))
        if k % 100 == 0:
            log(f"  tracés : {k}/{len(pairs)} paires traitées, {int((time.time() - t0) / 60)} min écoulées")

    # 2e passe : trajets impossibles -> on comble les petits trous OSM autour de la paire puis on retente
    rescued = 0
    for x, y, f, key in todo:
        la0, la1 = sorted((ST[x][0], ST[y][0]))
        lo0, lo1 = sorted((ST[x][1], ST[y][1]))
        n_br = G.bridge_gaps(la0 - BRIDGE_BUF, la1 + BRIDGE_BUF, lo0 - BRIDGE_BUF, lo1 + BRIDGE_BUF)
        path = G.route(snapped[x], snapped[y], f)
        if path:
            res[key] = build(x, y, path)
            rescued += 1
            log(f"  {key} : tracé rétabli après comblement de trou(s) OSM ({n_br} liaison(s) dans la zone)")
        else:
            fail.append(key)

    out = {}
    for x, y, f in pairs:
        key = f"{x}|{y}|{f}"
        if key in res:
            out[key] = res[key]
    log(f"{len(out)} tracés calculés (dont {rescued} rétablis par comblement de trous), "
        f"{len(fail)} sans tracé (le site relie alors les gares en ligne droite)")
    if fail:
        log("  sans tracé : " + ", ".join(fail))
    if len(out) < len(pairs) * 0.95:
        sys.exit("ERREUR : moins de 95 % des tracés calculés, fichier non écrit (l'ancien est conservé).")
    resj = {"v": 1, "generated": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
            "source": "© contributeurs OpenStreetMap (ODbL)", "e": out}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(resj, fh, separators=(",", ":"))
    log(f"{a.out} écrit ({os.path.getsize(a.out) // 1024} ko)")


if __name__ == "__main__":
    main()
