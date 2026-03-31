"""
app.py — CESTRIN Peek Monitor Platform
Backend Flask optimizat: cache în memorie, compresie gzip,
security headers, rate limiting avansat, protecție bot.
"""

import os, json, sqlite3, secrets, time, gzip, hashlib
from datetime import datetime, timedelta, UTC
from functools import wraps
from pathlib import Path
from flask import (Flask, request, jsonify, render_template,
                   g, make_response, redirect)
from pyproj import Transformer

try:
    import jwt, bcrypt
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    from geocoder import geocode_all_posts
except ImportError as e:
    print(f"Lipsă: {e}\nRulați: pip install -r requirements.txt")
    raise

# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__, template_folder='../frontend/templates')

app.config.update(
    SECRET_KEY       = os.environ.get('SECRET_KEY', secrets.token_hex(32)),
    JWT_EXPIRY_HOURS = int(os.environ.get('JWT_EXPIRY_HOURS', 8)),
    UPLOAD_API_KEY   = os.environ.get('UPLOAD_API_KEY', 'SCHIMBA_KEY_SECRET'),
    DB_PATH          = Path('data/cestrin.db'),
    STATUS_JSON      = Path('data/status.json'),
    STATUS_CACHE     = Path('data/status_cache.json.gz'),
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024,
)

# ─────────────────────────────────────────────
# CONVERSIE Stereo 70 → WGS84
# ─────────────────────────────────────────────
transformer = Transformer.from_crs("EPSG:3844", "EPSG:4326", always_xy=True)

def stereo70_to_wgs84(x, y):
    try:
        lon, lat = transformer.transform(x, y)
        return lat, lon
    except Exception:
        return None, None

# ── Rate Limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["300 per hour", "30 per minute"],
    storage_uri="memory://",
)

# ── Cache în memorie ──────────────────────────────────────────────────────────
_mem_cache: dict = {}   # { key: (data_bytes, etag, timestamp) }
CACHE_TTL = 60          # secunde

# ══════════════════════════════════════════════════════════════════════════════
# SECURITY HEADERS (pe fiecare răspuns)
# ══════════════════════════════════════════════════════════════════════════════

@app.after_request
def add_security_headers(resp):
    resp.headers.update({
        # Anti-clickjacking
        "X-Frame-Options":           "DENY",
        # Anti-XSS
        "X-Content-Type-Options":    "nosniff",
        "X-XSS-Protection":          "1; mode=block",
        # HTTPS obligatoriu (1 an)
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        # Content Security Policy
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
            "img-src 'self' data: https://*.tile.openstreetmap.org https://*.openstreetmap.org; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self';"
        ),
        # Referrer
        "Referrer-Policy":           "strict-origin-when-cross-origin",
        # Permissions
        "Permissions-Policy":        "geolocation=(), camera=(), microphone=()",
    })
    # Ascunde fingerprint server
    resp.headers.pop("Server", None)
    resp.headers.pop("X-Powered-By", None)
    return resp

# ══════════════════════════════════════════════════════════════════════════════
# BOT DETECTION
# ══════════════════════════════════════════════════════════════════════════════

BLOCKED_UA_FRAGMENTS = [
    'sqlmap', 'nikto', 'nmap', 'masscan', 'zgrab',
    'curl/', 'python-requests', 'go-http-client',
    'scrapy', 'wget/', 'libwww', 'jakarta',
    'dirbuster', 'gobuster', 'wfuzz', 'hydra',
]

BLOCKED_IPS: set = set()   # IPs blocate dinamic

@app.before_request
def bot_firewall():
    ip = request.remote_addr

    # ✅ PERMITE upload script intern
    if request.path == "/api/upload-status":
        return

    # IP blocat explicit
    if ip in BLOCKED_IPS:
        return make_response('', 444)   # No response (nginx style)

    # User-Agent suspect
    ua = request.headers.get('User-Agent', '').lower()
    if not ua:
        return make_response('', 444)

    for frag in BLOCKED_UA_FRAGMENTS:
        if frag in ua:
            BLOCKED_IPS.add(ip)
            return make_response('', 444)

    # Path scanning (tentative de descoperire directoare)
    path = request.path.lower()
    suspicious = [
        '.php', '.asp', '.aspx', '.env', '.git', '.htaccess',
        'wp-admin', 'wp-login', 'phpmyadmin', 'adminer',
        '../', 'etc/passwd', 'xmlrpc',
    ]
    for s in suspicious:
        if s in path:
            BLOCKED_IPS.add(ip)
            return make_response('', 444)

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DB_PATH'],
                               detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")   # performanță citire concurentă
        g.db.execute("PRAGMA synchronous=NORMAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    app.config['DB_PATH'].parent.mkdir(exist_ok=True)
    db = sqlite3.connect(app.config['DB_PATH'])
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'viewer',
            full_name  TEXT,
            email      TEXT,
            drdp       TEXT,
            active     INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ip        TEXT NOT NULL,
            username  TEXT,
            success   INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS upload_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            posturi_cnt INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now')),
            ip          TEXT,
            sursa       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_login_ip ON login_attempts(ip, timestamp);

        CREATE TABLE IF NOT EXISTS coords_manual (
            contor_id  TEXT PRIMARY KEY,
            lat        REAL NOT NULL,
            lng        REAL NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    db.commit()

    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        pw = bcrypt.hashpw(b'Cestrin2024!', bcrypt.gensalt()).decode()
        db.execute("""INSERT INTO users (username,password,role,full_name,drdp)
                      VALUES ('admin',?,'admin','Administrator CESTRIN','CESTRIN')""", (pw,))
        db.commit()
        print("✅ Admin creat. Parolă implicită: Cestrin2024!")

    db.close()

# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

def gen_token(user):
    return jwt.encode({
        'sub':       str(user['id']),
        'username':  user['username'],
        'role':      user['role'],
        'drdp':      user['drdp'] or '',
        'iat':       datetime.now(UTC),
        'exp':       datetime.now(UTC) + timedelta(hours=app.config['JWT_EXPIRY_HOURS']),
    }, app.config['SECRET_KEY'], algorithm='HS256')

def decode_token(token):
    try:
        return jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
    except:
        return None

def require_auth(role=None):
    def dec(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            token = None
            ah = request.headers.get('Authorization', '')
            if ah.startswith('Bearer '):
                token = ah[7:]
            if not token:
                token = request.cookies.get('t')
            if not token:
                return jsonify({'error': 'Autentificare necesară'}), 401
            payload = decode_token(token)
            if not payload:
                return jsonify({'error': 'Token expirat'}), 401
            if role and payload.get('role') != role:
                return jsonify({'error': 'Acces interzis'}), 403
            g.user = payload
            return f(*args, **kwargs)
        return wrapper
    return dec

def is_blocked(ip):
    db = get_db()
    cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    n = db.execute("""SELECT COUNT(*) FROM login_attempts
                      WHERE ip=? AND success=0 AND timestamp>?""",
                   (ip, cutoff)).fetchone()[0]
    return n >= 5

# ══════════════════════════════════════════════════════════════════════════════
# CACHE HELPER — răspunsuri comprimate gzip cu ETag
# ══════════════════════════════════════════════════════════════════════════════

def cached_json_response(key: str, data_fn):
    """
    Returnează răspuns JSON comprimat gzip cu ETag.
    data_fn() e apelat doar dacă cache-ul e expirat.
    """
    now = time.time()
    cached = _mem_cache.get(key)

    if cached and (now - cached[2]) < CACHE_TTL:
        body, etag = cached[0], cached[1]
    else:
        data = data_fn()
        body = gzip.compress(json.dumps(data, ensure_ascii=False).encode())
        etag = hashlib.md5(body).hexdigest()
        _mem_cache[key] = (body, etag, now)

    # ETag / 304 Not Modified
    if request.headers.get('If-None-Match') == etag:
        return make_response('', 304)

    resp = make_response(body)
    resp.headers.update({
        'Content-Type':     'application/json; charset=utf-8',
        'Content-Encoding': 'gzip',
        'ETag':             etag,
        'Cache-Control':    f'private, max-age={CACHE_TTL}',
        'Vary':             'Accept-Encoding',
    })
    return resp

def invalidate_cache(key: str = None):
    if key:
        _mem_cache.pop(key, None)
    else:
        _mem_cache.clear()

# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — Pagini
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/harta')
def harta():
    return render_template('harta.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

# Redirect www → non-www (sau invers, setat în Nginx de obicei)
@app.route('/robots.txt')
def robots():
    resp = make_response("User-agent: *\nDisallow: /api/\nDisallow: /admin\n")
    resp.content_type = "text/plain"
    return resp

# ══════════════════════════════════════════════════════════════════════════════
# API — Auth
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/login', methods=['POST'])
@limiter.limit("8 per minute; 30 per hour")
def api_login():
    ip   = request.remote_addr
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip().lower()[:64]
    password = data.get('password', '')[:128].encode()

    db = get_db()

    if is_blocked(ip):
        return jsonify({'error': 'Prea multe încercări. Așteptați 15 minute.'}), 429

    user = db.execute(
        "SELECT * FROM users WHERE username=? AND active=1", (username,)
    ).fetchone()

    ok = bool(user and bcrypt.checkpw(password, user['password'].encode()))

    db.execute("INSERT INTO login_attempts(ip,username,success) VALUES(?,?,?)",
               (ip, username, int(ok)))
    if ok:
        db.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user['id'],))
    db.commit()

    if not ok:
        return jsonify({'error': 'Credențiale incorecte'}), 401

    token = gen_token(dict(user))
    resp  = jsonify({
        'token':     token,
        'username':  user['username'],
        'full_name': user['full_name'],
        'role':      user['role'],
        'drdp':      user['drdp'],
    })
    resp.set_cookie('t', token, httponly=True, secure=False,
                    samesite='Lax', max_age=app.config['JWT_EXPIRY_HOURS']*3600)
    return resp

@app.route('/api/logout', methods=['POST'])
def api_logout():
    resp = jsonify({'ok': True})
    resp.delete_cookie('t')
    return resp

@app.route('/api/me')
@require_auth()
def api_me():
    return jsonify(g.user)

# ══════════════════════════════════════════════════════════════════════════════
# API — Date contoare (cu cache + gzip + ETag)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/status')
@require_auth()
def api_status():
    def load():
        p = app.config['STATUS_JSON']
        if not p.exists():
            return {'contoare': [], 'ultima_actualizare': None}
        with open(p, encoding='utf-8') as f:
            data = json.load(f)

        # Coordonate plasate manual (prioritate maximă)
        db = get_db()
        manual = {
            r['contor_id']: {'lat': r['lat'], 'lng': r['lng']}
            for r in db.execute(
                "SELECT contor_id, lat, lng FROM coords_manual"
            ).fetchall()
        }

        for c in data.get('contoare', []):
            cid = str(c.get('id', ''))

            # 1. Coordonate manuale — prioritate maximă
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

@app.route('/api/status/summary')
@require_auth()
def api_summary():
    """Rezumat rapid pentru header — doar contorizare pe stări."""
    def load():
        p = app.config['STATUS_JSON']
        if not p.exists():
            return {}
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        counts = {}
        for c in data.get('contoare', []):
            s = c.get('stare', 'unknown')
            counts[s] = counts.get(s, 0) + 1
        counts['total']             = len(data.get('contoare', []))
        counts['ultima_actualizare'] = data.get('ultima_actualizare')
        return counts

    return cached_json_response('summary', load)

# ══════════════════════════════════════════════════════════════════════════════
# API — Upload date (de la scriptul Python CESTRIN)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/upload-status', methods=['POST'])
@limiter.limit("30 per hour")
def api_upload():
    key = request.headers.get('X-API-Key', '')
    if not secrets.compare_digest(key, app.config['UPLOAD_API_KEY']):
        return jsonify({'error': 'Cheie invalidă'}), 403

    data = request.get_json(silent=True)
    if not data or not isinstance(data.get('contoare'), list):
        return jsonify({'error': 'Format invalid'}), 400

    contoare = data['contoare']
    if len(contoare) == 0:
        return jsonify({'error': 'Listă goală'}), 400

    # ─────────────────────────────────────────────
    # Conversie coordonate Stereo 70 → WGS84
    # ─────────────────────────────────────────────
    for c in contoare:
        # dacă există coordonate Stereo 70
        if c.get('x') and c.get('y'):
            lat, lng = stereo70_to_wgs84(c['x'], c['y'])
            if lat and lng:
                c['lat'] = lat
                c['lng'] = lng

    # fallback: geocodare DOAR dacă nu avem coordonate
    needs_geo = [c for c in contoare if not c.get('lat') or not c.get('lng')]
    if needs_geo:
        geocode_all_posts(needs_geo)

    # Normalizare stări — stările valide pentru contoarele PEEK/VEK
    valid = {
        'clasificator', 'totalizator', 'defect',
        'nefunctional', 'fara_conexiune', 'unknown'
    }
    for c in contoare:
        if c.get('stare') not in valid:
            c['stare'] = 'unknown'

    data['ultima_actualizare'] = datetime.now(UTC).isoformat() + 'Z'

    with open(app.config['STATUS_JSON'], 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Log + invalidare cache
    db = get_db()
    db.execute("INSERT INTO upload_log(posturi_cnt,ip,sursa) VALUES(?,?,?)",
               (len(contoare), request.remote_addr, data.get('sursa', '?')))
    db.commit()
    invalidate_cache()

    return jsonify({'ok': True, 'posturi': len(contoare)})

# ══════════════════════════════════════════════════════════════════════════════
# API — Admin
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/admin/users')
@require_auth(role='admin')
def api_users():
    rows = get_db().execute("""
        SELECT id,username,role,full_name,email,drdp,active,created_at,last_login
        FROM users ORDER BY drdp,username""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/users', methods=['POST'])
@require_auth(role='admin')
@limiter.limit("20 per hour")
def api_create_user():
    d  = request.get_json(silent=True) or {}
    un = d.get('username','').strip().lower()[:64]
    pw = d.get('password','')[:128]
    rl = d.get('role','viewer')
    fn = d.get('full_name','')[:128]
    em = d.get('email','')[:128]
    dr = d.get('drdp','')[:64]

    if not un or not pw:
        return jsonify({'error': 'Username și parolă obligatorii'}), 400
    if rl not in ('admin','viewer'):
        return jsonify({'error': 'Rol invalid'}), 400
    if len(pw) < 8:
        return jsonify({'error': 'Parolă prea scurtă (min 8)'}), 400

    ph = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    try:
        db = get_db()
        db.execute("""INSERT INTO users(username,password,role,full_name,email,drdp)
                      VALUES(?,?,?,?,?,?)""", (un,ph,rl,fn,em,dr))
        db.commit()
        return jsonify({'ok': True}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Username există deja'}), 409

@app.route('/api/admin/users/<int:uid>', methods=['PATCH'])
@require_auth(role='admin')
def api_patch_user(uid):
    d  = request.get_json(silent=True) or {}
    db = get_db()
    if 'active' in d:
        db.execute("UPDATE users SET active=? WHERE id=?", (int(d['active']), uid))
    if 'role' in d and d['role'] in ('admin','viewer'):
        db.execute("UPDATE users SET role=? WHERE id=?", (d['role'], uid))
    if 'password' in d and len(d['password']) >= 8:
        ph = bcrypt.hashpw(d['password'].encode(), bcrypt.gensalt()).decode()
        db.execute("UPDATE users SET password=? WHERE id=?", (ph, uid))
    db.commit()
    return jsonify({'ok': True})

@app.route('/api/admin/upload-log')
@require_auth(role='admin')
def api_upload_log():
    rows = get_db().execute("""
        SELECT * FROM upload_log ORDER BY uploaded_at DESC LIMIT 100
    """).fetchall()
    return jsonify([dict(r) for r in rows])

# ══════════════════════════════════════════════════════════════════════════════
# API — Coordonate manuale (plasare pe hartă prin click/drag)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/coords', methods=['POST'])
@require_auth()
def api_save_coords():
    """Salvează coordonate plasate manual pe hartă (click sau drag)."""
    data      = request.get_json(silent=True) or {}
    contor_id = str(data.get('id', '')).strip()[:20]
    lat       = data.get('lat')
    lng       = data.get('lng')

    if not contor_id or lat is None or lng is None:
        return jsonify({'error': 'id, lat și lng sunt obligatorii'}), 400

    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return jsonify({'error': 'lat/lng trebuie să fie numere'}), 400

    # Validare bounding box România + vecinătăți
    if not (43.5 <= lat <= 48.5 and 20.0 <= lng <= 30.0):
        return jsonify({'error': 'Coordonate în afara României'}), 400

    db = get_db()
    db.execute("""
        INSERT INTO coords_manual (contor_id, lat, lng, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(contor_id) DO UPDATE SET
            lat        = excluded.lat,
            lng        = excluded.lng,
            updated_at = excluded.updated_at
    """, (contor_id, lat, lng))
    db.commit()
    invalidate_cache()

    return jsonify({'ok': True, 'id': contor_id, 'lat': lat, 'lng': lng})


@app.route('/api/coords', methods=['GET'])
@require_auth()
def api_get_coords():
    """Returnează toate coordonatele salvate manual."""
    rows = get_db().execute(
        "SELECT contor_id, lat, lng, updated_at FROM coords_manual ORDER BY updated_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/coords/<contor_id>', methods=['DELETE'])
@require_auth(role='admin')
def api_delete_coords(contor_id):
    """Șterge coordonatele manuale ale unui contor (admin only)."""
    db = get_db()
    db.execute("DELETE FROM coords_manual WHERE contor_id = ?", (contor_id,))
    db.commit()
    invalidate_cache()
    return jsonify({'ok': True})

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    init_db()
    print("=" * 50)
    print("  CESTRIN Peek Monitor")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(debug=False, host='0.0.0.0', port=5000)
