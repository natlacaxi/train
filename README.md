# Plan du réseau TER PACA

Carte interactive du réseau TER Provence-Alpes-Côte d'Azur (GitHub Pages).

## Structure du dépôt
```
index.html            application (tout le plan)
sw.js                 service worker (hors connexion)
manifest.webmanifest  installation sur mobile
favicon.ico / favicon.png / apple-touch-icon.png / icon-192.png / icon-512.png / icon-maskable-512.png
robots.txt / sitemap.xml / .nojekyll
build_horaires.py     génère horaires-paca.json, horaires-france.json, stops-france.json
build_incidents.py    génère incidents.json
build_geometrie.py    génère geom-lignes.json (tracé réel des voies, OpenStreetMap)
geom-paires.json      liste des gares et paires de gares (fourni, ne pas modifier)
.github/workflows/horaires.yml    mise à jour des horaires (chaque nuit)
.github/workflows/incidents.yml   mise à jour des incidents (toutes les 20 min)
.github/workflows/geometrie.yml   tracé des voies (1 fois par mois)
```
Les fichiers `.json` sont créés automatiquement par les workflows : ne pas les écrire à la main.

## Mise en service
1. Settings → Pages → Deploy from a branch → `main` / root.
2. Settings → Actions → General → Workflow permissions → **Read and write permissions**.
3. Actions → « Mise à jour des horaires » → Run workflow (attendre le vert), puis « Incidents » → Run workflow, puis « Tracé des voies (OpenStreetMap) » → Run workflow (10 à 40 min, une seule fois).

Données : SNCF Open Data (licence ODbL), transport.data.gouv.fr, © contributeurs OpenStreetMap (ODbL).
