"""
geocoder.py — Localizare automată posturi după DN + km + localitate
────────────────────────────────────────────────────────────────────
Strategie (în ordine de precizie):
  1. Cache local JSON — dacă postul a fost geocodat anterior
  2. Interpolare pe traseu DN — din tabelul de referință DN
  3. Nominatim OSM — geocoding după localitate + județ
  4. Centrul României fallback (nu blochează sistemul)
"""

import json
import time
import math
import logging
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

log = logging.getLogger("geocoder")

CACHE_FILE = Path("data/geocache.json")

# ══════════════════════════════════════════════════════════════════════════════
# TABEL DN — puncte de referință (start km 0 și câteva puncte intermediare)
# Format: DN -> listă de (km, lat, lng)
# Valori aproximative pentru interpolarea liniară
# ══════════════════════════════════════════════════════════════════════════════
DN_REFERENCE = {
    "DN1":  [(0,  44.432, 26.103), (50,  44.891, 25.982), (100, 45.302, 25.728),
             (150, 45.634, 25.021), (200, 45.928, 24.422), (250, 46.212, 23.821),
             (300, 46.521, 23.456), (350, 46.756, 23.355), (400, 46.921, 23.156),
             (450, 47.055, 22.944), (500, 47.066, 21.921), (550, 47.032, 21.021)],
    "DN1A": [(0,  44.432, 26.103), (50,  45.123, 25.987), (100, 45.456, 25.543)],
    "DN1B": [(0,  44.980, 26.021), (50,  45.234, 25.876), (100, 45.678, 25.432)],
    "DN1C": [(0,  46.756, 23.590), (30,  47.012, 23.876), (60,  47.131, 23.874),
             (90,  47.345, 24.123), (120, 47.567, 24.234)],
    "DN2":  [(0,  44.432, 26.103), (50,  44.876, 26.543), (100, 45.234, 26.876),
             (150, 45.567, 27.123), (200, 45.890, 27.456), (250, 46.012, 27.876)],
    "DN7":  [(0,  44.432, 26.103), (50,  44.234, 25.432), (100, 44.121, 24.876),
             (150, 44.045, 24.234), (200, 45.123, 23.654), (250, 45.456, 23.321),
             (300, 45.678, 22.987), (350, 45.890, 22.543), (400, 45.987, 22.012)],
    "DN7C": [(0,  45.321, 24.432), (30,  45.543, 24.654), (60,  45.765, 24.765)],
    "DN13": [(0,  46.234, 24.789), (50,  46.456, 25.012), (100, 46.678, 25.234),
             (150, 46.890, 25.543), (200, 46.654, 25.987)],
    "DN14": [(0,  45.876, 24.123), (50,  46.123, 24.345), (100, 46.345, 24.567)],
    "DN15": [(0,  46.543, 26.921), (50,  46.789, 26.543), (100, 46.921, 26.012),
             (150, 47.012, 25.432), (200, 47.234, 24.876), (250, 47.345, 24.234)],
    "DN16": [(0,  46.756, 23.590), (30,  47.012, 23.987), (60,  47.137, 24.497)],
    "DN17": [(0,  47.131, 23.874), (50,  47.234, 24.123), (100, 47.287, 24.409),
             (150, 47.456, 24.765), (200, 47.567, 25.012)],
    "DN18": [(0,  47.658, 23.568), (50,  47.678, 24.012), (100, 47.234, 24.543)],
    "DN1E": [(0,  47.066, 21.921), (50,  46.876, 21.543), (100, 46.543, 21.234)],
    "DN1F": [(0,  46.756, 23.590), (30,  46.876, 23.012), (60,  46.876, 22.931)],
    "DN6":  [(0,  44.432, 26.103), (50,  44.234, 25.432), (100, 44.012, 24.765),
             (150, 43.876, 24.012), (200, 43.765, 23.321), (250, 43.654, 22.654),
             (300, 43.876, 22.012), (350, 44.123, 21.432), (400, 44.456, 21.012)],
    "DN75": [(0,  46.567, 23.783), (30,  46.456, 23.345), (60,  46.368, 23.046)],
    "DN66": [(0,  45.456, 23.432), (50,  45.234, 23.123), (100, 45.012, 22.876)],
    "DN67": [(0,  44.876, 23.876), (50,  45.012, 23.543), (100, 45.234, 23.234)],
    "DN12": [(0,  46.234, 25.789), (30,  45.987, 25.876), (60,  45.866, 25.789)],
    "DN11": [(0,  45.654, 25.876), (50,  45.876, 26.012), (100, 46.012, 26.234)],
    "DN25": [(0,  45.432, 27.789), (50,  45.678, 27.543), (100, 45.876, 27.234)],
}


def _interpolate_on_dn(dn: str, km: float) -> tuple[float, float] | None:
    """Interpolează liniar coordonatele pe traseul DN."""
    dn_key = dn.upper().replace(" ", "")
    points = DN_REFERENCE.get(dn_key)
    if not points:
        return None

    # Caută intervalul
    for i in range(len(points) - 1):
        k0, lat0, lng0 = points[i]
        k1, lat1, lng1 = points[i + 1]
        if k0 <= km <= k1:
            t = (km - k0) / (k1 - k0)
            return (lat0 + t * (lat1 - lat0),
                    lng0 + t * (lng1 - lng0))

    # Extrapolează dacă km e în afara intervalului
    if km < points[0][0]:
        return (points[0][1], points[0][2])
    return (points[-1][1], points[-1][2])


def _geocode_nominatim(localitate: str, judet: str = "") -> tuple[float, float] | None:
    """Geocodare prin OpenStreetMap Nominatim (gratuit, fără cheie API)."""
    if not requests:
        return None

    query = f"{localitate}, {judet}, Romania" if judet else f"{localitate}, Romania"

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "ro"},
            headers={"User-Agent": "CESTRIN-PeekMonitor/1.0"},
            timeout=10,
        )
        data = resp.json()
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception as e:
        log.warning(f"Nominatim eroare pentru '{localitate}': {e}")

    return None


# ══════════════════════════════════════════════════════════════════════════════
# CACHE
# ══════════════════════════════════════════════════════════════════════════════

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# FUNCȚIE PRINCIPALĂ
# ══════════════════════════════════════════════════════════════════════════════

def geocode_post(post_id: str, dn: str, km, localitate: str,
                 judet: str = "", force_refresh: bool = False) -> tuple[float, float]:
    """
    Returnează (lat, lng) pentru un post de trafic.
    Strategii în ordine: cache → DN interpolare → Nominatim → fallback centru RO.
    """
    cache = _load_cache()
    cache_key = f"{post_id}_{dn}_{km}"

    if not force_refresh and cache_key in cache:
        coords = cache[cache_key]
        return (coords["lat"], coords["lng"])

    lat, lng = None, None

    # 1. Interpolare DN
    if dn and km is not None:
        try:
            result = _interpolate_on_dn(dn, float(km))
            if result:
                lat, lng = result
                log.info(f"  {post_id}: coordonate prin interpolare DN ({lat:.4f}, {lng:.4f})")
        except (ValueError, TypeError):
            pass

    # 2. Nominatim fallback
    if lat is None and localitate:
        result = _geocode_nominatim(localitate, judet)
        if result:
            lat, lng = result
            log.info(f"  {post_id}: coordonate prin Nominatim ({lat:.4f}, {lng:.4f})")
        time.sleep(1.1)  # Respectă rate limit Nominatim (1 req/sec)

    # 3. Centrul României (fallback final)
    if lat is None:
        lat, lng = 45.9432, 24.9668
        log.warning(f"  {post_id}: coordonate lipsă, folosesc centrul României")

    # Salvare cache
    cache[cache_key] = {"lat": lat, "lng": lng, "sursa": "dn" if dn else "nominatim"}
    _save_cache(cache)

    return (lat, lng)


def geocode_all_posts(posturi: list[dict]) -> list[dict]:
    """Geocodează toată lista de posturi, adaugă lat/lng la fiecare."""
    log.info(f"Geocodare {len(posturi)} posturi...")
    cache = _load_cache()
    needs_nominatim = []

    for post in posturi:
        cache_key = f"{post['id']}_{post.get('drum','')}_{post.get('km','')}"

        if cache_key in cache:
            post["lat"] = cache[cache_key]["lat"]
            post["lng"] = cache[cache_key]["lng"]
            continue

        # Încearcă DN interpolare
        result = None
        if post.get("drum") and post.get("km") is not None:
            try:
                result = _interpolate_on_dn(post["drum"], float(post["km"]))
            except (ValueError, TypeError):
                pass

        if result:
            post["lat"], post["lng"] = result
            cache[cache_key] = {"lat": result[0], "lng": result[1], "sursa": "dn"}
        else:
            needs_nominatim.append((post, cache_key))

    # Salvare cache după DN
    _save_cache(cache)

    # Nominatim pentru ce a rămas (cu rate limiting)
    if needs_nominatim:
        log.info(f"Nominatim geocoding pentru {len(needs_nominatim)} posturi...")
        for post, cache_key in needs_nominatim:
            result = _geocode_nominatim(
                post.get("localitate", ""),
                post.get("judet", "")
            )
            if result:
                post["lat"], post["lng"] = result
                cache[cache_key] = {"lat": result[0], "lng": result[1], "sursa": "nominatim"}
            else:
                post["lat"], post["lng"] = 45.9432, 24.9668
                cache[cache_key] = {"lat": 45.9432, "lng": 24.9668, "sursa": "fallback"}
            time.sleep(1.1)

        _save_cache(cache)

    log.info("Geocodare finalizată.")
    return posturi
