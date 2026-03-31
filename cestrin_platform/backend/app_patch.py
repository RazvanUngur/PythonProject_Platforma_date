"""
PATCH pentru app.py — adăugați aceste bucăți în fișierul vostru app.py existent

1. Stările noi (înlocuiți validarea din api_upload)
2. Endpoint nou: POST /api/coords  — salvează coordonate plasate manual pe hartă
3. Endpoint nou: GET  /api/coords  — returnează toate coordonatele salvate manual
"""

# ══════════════════════════════════════════════════════════════════════════════
# 1. În init_db(), adăugați acest tabel după celelalte CREATE TABLE:
# ══════════════════════════════════════════════════════════════════════════════
PATCH_INIT_DB = """
    CREATE TABLE IF NOT EXISTS coords_manual (
        contor_id  TEXT PRIMARY KEY,
        lat        REAL NOT NULL,
        lng        REAL NOT NULL,
        sursa      TEXT DEFAULT 'manual',
        updated_at TEXT DEFAULT (datetime('now'))
    );
"""
# Adăugați această linie în blocul db.executescript(...) din init_db()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Stări valide noi — înlocuiți în api_upload():
#    valid = {'ok','partial','offline','maintenance','unknown'}
#    cu:
# ══════════════════════════════════════════════════════════════════════════════
STARI_VALIDE = {
    'clasificator', 'totalizator', 'defect',
    'nefunctional', 'fara_conexiune', 'unknown'
}


# ══════════════════════════════════════════════════════════════════════════════
# 3. Endpoint nou — salvare coordonate manuale
#    Adăugați această funcție în app.py
# ══════════════════════════════════════════════════════════════════════════════

PATCH_ENDPOINT = '''
@app.route('/api/coords', methods=['POST'])
@require_auth()
def api_save_coords():
    """Salvează coordonate plasate manual pe hartă (drag & drop)."""
    data = request.get_json(silent=True) or {}
    contor_id = str(data.get('id', '')).strip()[:20]
    lat = data.get('lat')
    lng = data.get('lng')

    if not contor_id or lat is None or lng is None:
        return jsonify({'error': 'id, lat, lng obligatorii'}), 400

    # Validare coordonate în boundingbox România + vecinătăți
    if not (43.5 <= float(lat) <= 48.5 and 20.0 <= float(lng) <= 30.0):
        return jsonify({'error': 'Coordonate în afara României'}), 400

    db = get_db()
    db.execute("""
        INSERT INTO coords_manual (contor_id, lat, lng, sursa, updated_at)
        VALUES (?, ?, ?, 'manual', datetime('now'))
        ON CONFLICT(contor_id) DO UPDATE SET
            lat        = excluded.lat,
            lng        = excluded.lng,
            updated_at = excluded.updated_at
    """, (contor_id, float(lat), float(lng)))
    db.commit()
    invalidate_cache()

    return jsonify({'ok': True, 'id': contor_id, 'lat': lat, 'lng': lng})


@app.route('/api/coords', methods=['GET'])
@require_auth()
def api_get_coords():
    """Returnează toate coordonatele salvate manual."""
    rows = get_db().execute(
        "SELECT contor_id, lat, lng, updated_at FROM coords_manual"
    ).fetchall()
    return jsonify({r['contor_id']: {'lat': r['lat'], 'lng': r['lng']} for r in rows})
'''


# ══════════════════════════════════════════════════════════════════════════════
# 4. În api_status() — îmbinați coordonatele manuale cu datele din JSON
#    Înlocuiți funcția load() din api_status() cu aceasta:
# ══════════════════════════════════════════════════════════════════════════════

PATCH_API_STATUS = '''
@app.route('/api/status')
@require_auth()
def api_status():
    def load():
        p = app.config['STATUS_JSON']
        if not p.exists():
            return {'contoare': [], 'ultima_actualizare': None}
        with open(p, encoding='utf-8') as f:
            data = json.load(f)

        # Coordonate manuale din DB (au prioritate față de X/Y din Excel)
        db = get_db()
        manual = {
            r['contor_id']: {'lat': r['lat'], 'lng': r['lng']}
            for r in db.execute("SELECT contor_id, lat, lng FROM coords_manual").fetchall()
        }

        for c in data.get('contoare', []):
            cid = c.get('id', '')

            # 1. Coordonate manuale (prioritate maximă)
            if cid in manual:
                c['lat'] = manual[cid]['lat']
                c['lng'] = manual[cid]['lng']
                c['coords_sursa'] = 'manual'
                continue

            # 2. Conversie Stereo70 din Excel
            if c.get('x') and c.get('y') and not c.get('lat'):
                lat, lng = stereo70_to_wgs84(c['x'], c['y'])
                if lat and lng:
                    c['lat'] = lat
                    c['lng'] = lng
                    c['coords_sursa'] = 'stereo70'

        return data

    return cached_json_response('status', load)
'''

if __name__ == '__main__':
    print("Acesta nu este un script de rulat direct.")
    print("Copiați bucățile de cod de mai sus în app.py conform instrucțiunilor.")
