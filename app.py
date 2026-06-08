"""
Diplomacia Bot - SaaS Platform
تم التعديل: OAuth2 مع redirect_uri ثابت HTTPS
"""
import os, json, time, threading, logging, hashlib, secrets
from datetime import datetime
from flask import Flask, jsonify, request, Response, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
from psycopg2.extras import RealDictCursor
from authlib.integrations.flask_client import OAuth

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'saas-diplo-2024')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL', '')
BASE_URL = "https://diplomacia.com.tr/api"

PERKS = {
    'barracks':       {'label': 'BARRACKS',       'key': 'kisla'},
    'war_techniques': {'label': 'WAR TECHNIQUES', 'key': 'savas_teknikleri'},
    'scientist':      {'label': 'SCIENTIST',       'key': 'bilim_insani'},
}

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'admin123')

# ========== GOOGLE OAuth Configuration ==========
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '33794621919-004btps78s3sooo0u9vu2em9gl4udip8.apps.googleusercontent.com')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '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')

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile'
    }
)

# ========== تعريف HTML (ضع النسخة الكاملة من ملفك الأصلي هنا) ==========
# أنا سأكتبها بشكل رمزي، يجب أن تستبدل المحتوى بين الاقتباسات الثلاثية بالنسخة الأصلية الكاملة
ADMIN_HTML = r"""<!DOCTYPE html>... (ضع المحتوى الكامل الأصلي) ..."""
USER_HTML = r"""<!DOCTYPE html>... (ضع المحتوى الكامل الأصلي) ..."""
LOGIN_HTML = r"""<!DOCTYPE html>... (ضع المحتوى الكامل الأصلي) ..."""

# ── DB ─────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    return conn

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS'),
            is_active INTEGER DEFAULT 1
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            token TEXT DEFAULT '',
            name TEXT DEFAULT '',
            perk TEXT DEFAULT 'scientist',
            currency TEXT DEFAULT 'diamond',
            avatar TEXT DEFAULT '',
            balance TEXT DEFAULT '—',
            diamonds TEXT DEFAULT '—',
            level_num TEXT DEFAULT '?',
            xp_pct INTEGER DEFAULT 0,
            lv_barracks TEXT DEFAULT '?',
            lv_war TEXT DEFAULT '?',
            lv_scientist TEXT DEFAULT '?',
            upgrades INTEGER DEFAULT 0,
            last_upgrade TEXT DEFAULT '—',
            UNIQUE(user_id, slot)
        )""")
        conn.commit()
    log.info("DB initialized")

init_db()

def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()

def db_fetchone(query, params=()):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchone()

def db_fetchall(query, params=()):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()

def db_exec(query, params=()):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()

def get_user(username):
    return db_fetchone("SELECT * FROM users WHERE username=%s", (username,))

def get_accounts(user_id):
    rows = db_fetchall("SELECT * FROM accounts WHERE user_id=%s ORDER BY slot", (user_id,))
    existing = {r['slot'] for r in rows}
    for slot in [1, 2]:
        if slot not in existing:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO accounts (user_id, slot) VALUES (%s,%s) ON CONFLICT DO NOTHING", (user_id, slot))
                conn.commit()
    return db_fetchall("SELECT * FROM accounts WHERE user_id=%s ORDER BY slot", (user_id,))

def save_account(user_id, slot, **kwargs):
    fields = ', '.join(f"{k}=%s" for k in kwargs)
    vals = list(kwargs.values()) + [user_id, slot]
    db_exec(f"UPDATE accounts SET {fields} WHERE user_id=%s AND slot=%s", vals)

# ── Runtime state ──────────────────────────────────
runtime = {}
stop_events = {}
bot_threads = {}

def rt_key(uid, slot): return f"{uid}_{slot}"

def get_rt(uid, slot):
    k = rt_key(uid, slot)
    if k not in runtime:
        runtime[k] = {'status':'stopped','cooldown':0,'enabled':False,'logs':[]}
    return runtime[k]

def add_log(uid, slot, msg, level='info'):
    rt = get_rt(uid, slot)
    ts = datetime.now().strftime('%H:%M:%S')
    entry = {'time':ts,'msg':msg,'level':level}
    rt['logs'].insert(0, entry)
    rt['logs'] = rt['logs'][:60]
    log.info(f"[U{uid}/S{slot}] {msg}")
    socketio.emit('log', {'slot':slot,'entry':entry}, room=f"user_{uid}")
    socketio.emit('update', build_state(uid), room=f"user_{uid}")

def build_state(uid):
    accs = get_accounts(uid)
    result = {}
    for acc in accs:
        slot = acc['slot']
        rt = get_rt(uid, slot)
        result[str(slot)] = {
            'slot': slot,
            'token': bool(acc['token']),
            'name': acc['name'] or f'حساب {slot}',
            'perk': acc['perk'],
            'currency': acc['currency'],
            'balance': acc['balance'],
            'diamonds': acc['diamonds'],
            'level_num': acc['level_num'],
            'xp_pct': acc['xp_pct'],
            'level': {
                'barracks': acc['lv_barracks'],
                'war_techniques': acc['lv_war'],
                'scientist': acc['lv_scientist'],
            },
            'upgrades': acc['upgrades'],
            'last_upgrade': acc['last_upgrade'],
            'status': rt['status'],
            'enabled': rt['enabled'],
            'cooldown': rt['cooldown'],
        }
    return result

# ── API Helpers ────────────────────────────────────
def make_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0',
        'Origin': 'https://diplomacia.com.tr',
        'Referer': 'https://diplomacia.com.tr/',
    }

def api_get(token, path):
    import requests as req
    try:
        r = req.get(f"{BASE_URL}{path}", headers=make_headers(token), timeout=15)
        if r.status_code == 200: return r.json()
        log.warning(f"GET {path} → {r.status_code}")
        return None
    except Exception as e:
        log.error(f"GET {path} err: {e}")
        return None

def api_post(token, path, data=None):
    import requests as req
    try:
        r = req.post(f"{BASE_URL}{path}", headers=make_headers(token), json=data or {}, timeout=15)
        return r.status_code, r.json() if r.content else {}
    except Exception as e:
        log.error(f"POST {path} err: {e}")
        return 0, {}

def refresh_profile(uid, slot):
    acc = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    if not acc or not acc['token']: return False
    data = api_get(acc['token'], '/players/profile')
    if not data: return False
    try:
        p = data.get('player', data)
        skills = p.get('skills', {})
        lp = p.get('levelProgress', {})
        pct = lp.get('percentage', 0)
        xp = round(pct * 100) if isinstance(pct, float) and pct <= 1 else round(pct)
        def lv(key):
            cur = skills.get(key, '?')
            pend = skills.get(f'{key}_pending')
            if pend and pend != cur:
                return f'{cur}→{pend}'
            return str(cur)
        save_account(uid, slot,
            name=p.get('username', acc['name'] or f'حساب {slot}'),
            balance=f"${p.get('balance',0):,}",
            diamonds=str(p.get('diamonds',0)),
            level_num=str(p.get('level','?')),
            xp_pct=xp,
            lv_barracks=lv('kisla'),
            lv_war=lv('savas_teknikleri'),
            lv_scientist=lv('bilim_insani'),
        )
        socketio.emit('update', build_state(uid), room=f"user_{uid}")
        return True
    except Exception as e:
        log.error(f"Profile parse err: {e}")
        return False

def _iso_to_remaining(ts_str):
    if not ts_str: return 0
    try:
        from datetime import timezone
        ts_str = ts_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc)
        remaining = (dt - now).total_seconds()
        return max(0, int(remaining))
    except Exception as e:
        log.error(f"[ISO parse] {ts_str}: {e}")
        return 0

def get_cooldown(uid, slot, perk_key):
    acc = db_fetchone("SELECT token FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    if not acc: return None
    token = acc['token']
    data = api_get(token, '/players/profile')
    if not data: return None
    try:
        p = data.get('player', data)
        skills = p.get('skills', {})
        pending_at = skills.get(f'{perk_key}_pending_at')
        if pending_at:
            remaining = _iso_to_remaining(pending_at)
            if remaining > 0:
                return remaining
            return 0
        pending = skills.get(f'{perk_key}_pending')
        if pending is not None:
            return 60
        return 0
    except Exception as e:
        log.error(f"[CD] error: {e}")
        return 0

def do_upgrade(uid, slot, perk_key, currency):
    acc = db_fetchone("SELECT token FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    if not acc: return False, 'no account'
    token = acc['token']
    payload = {'skill': perk_key, 'type': currency}
    status, resp = api_post(token, '/players/skills/upgrade', payload)
    log.info(f"Upgrade U{uid}/S{slot} {payload}: {status} {str(resp)[:200]}")
    if status in (200, 201): return True, resp
    if status == 401: return False, 'Token منتهي الصلاحية'
    msg = resp.get('message', '') if isinstance(resp, dict) else str(resp)
    full = msg + str(resp)
    RATE_LIMIT = ['çok hızlı', 'too many', 'rate limit', 'bekleyin', 'wait', 'dakika']
    if any(p.lower() in full.lower() for p in RATE_LIMIT):
        retry_after = resp.get('retryAfter', 65) if isinstance(resp, dict) else 65
        return 'rate_limit', retry_after
    ALREADY = ['Başka bir beceri', 'already upgrading', 'devam ediyor', 'upgrade in progress']
    if any(p.lower() in full.lower() for p in ALREADY):
        active = resp.get('active_skill', perk_key) if isinstance(resp, dict) else perk_key
        return 'already_upgrading', active
    return False, msg or str(resp)

def bot_loop(uid, slot, stop_ev):
    acc = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    if not acc: return
    rt = get_rt(uid, slot)
    rt['status'] = 'running'
    rt['enabled'] = True
    if refresh_profile(uid, slot):
        a = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
        add_log(uid, slot, f"✅ متصل — {a['name']} | {a['balance']} | 💎{a['diamonds']}", 'ok')
    else:
        add_log(uid, slot, '⚠️ Token منتهي أو خاطئ', 'warn')
        rt['status'] = 'error'; rt['enabled'] = False
        socketio.emit('update', build_state(uid), room=f"user_{uid}")
        return
    acc = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    init_perk = acc['perk']
    init_cd = get_cooldown(uid, slot, PERKS[init_perk]['key'])
    if init_cd and init_cd > 0:
        rt['cooldown'] = init_cd
        add_log(uid, slot, f"⏳ {PERKS[init_perk]['label']} — cooldown: {fmt(init_cd)}", 'warn')
    else:
        add_log(uid, slot, f"▶ البوت شغّال — {PERKS[init_perk]['label']} جاهز", 'ok')
    socketio.emit('update', build_state(uid), room=f"user_{uid}")
    fail_count = 0
    while not stop_ev.is_set():
        try:
            acc = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
            if not acc: break
            currency = acc['currency']
            perk = acc['perk']
            perk_key = PERKS[perk]['key']
            perk_label = PERKS[perk]['label']
            cd = get_cooldown(uid, slot, perk_key)
            if cd is None:
                fail_count += 1
                if fail_count >= 5:
                    add_log(uid, slot, '❌ فشل 5 مرات متتالية — توقف', 'error'); break
                add_log(uid, slot, f'⚠️ فشل قراءة الـ API ({fail_count}/5)', 'warn')
                time.sleep(60); continue
            fail_count = 0
            if cd > 0:
                rt['cooldown'] = cd
                add_log(uid, slot, f"⏳ {perk_label} — كمل {fmt(cd)}", 'warn')
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
                while not stop_ev.is_set():
                    if rt['cooldown'] <= 0: break
                    time.sleep(1)
                    rt['cooldown'] = max(0, rt['cooldown'] - 1)
                    if rt['cooldown'] > 0 and rt['cooldown'] % 60 == 0:
                        fresh_cd = get_cooldown(uid, slot, perk_key)
                        if fresh_cd is not None:
                            rt['cooldown'] = max(0, fresh_cd)
                continue
            rt['cooldown'] = 0
            add_log(uid, slot, f"⚡ {perk_label} جاهز — جاري الترقية...", 'ok')
            socketio.emit('update', build_state(uid), room=f"user_{uid}")
            success, result = do_upgrade(uid, slot, perk_key, currency)
            if success is True:
                db_exec("UPDATE accounts SET upgrades=upgrades+1, last_upgrade=%s WHERE user_id=%s AND slot=%s",
                        (datetime.now().strftime('%H:%M:%S'), uid, slot))
                add_log(uid, slot, f"✅ تمت ترقية {perk_label}!", 'ok')
                refresh_profile(uid, slot)
                time.sleep(3)
                real_cd = get_cooldown(uid, slot, perk_key)
                rt['cooldown'] = real_cd if (real_cd and real_cd > 0) else 65
                add_log(uid, slot, f"⏳ cooldown: {fmt(rt['cooldown'])}", 'info')
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
            elif success == 'rate_limit':
                wait = int(result) + 5
                add_log(uid, slot, f"⏳ طلبات كتير — انتظار {wait}s", 'warn')
                rt['cooldown'] = wait
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
                time.sleep(wait)
            elif success == 'already_upgrading':
                add_log(uid, slot, f"⏳ ترقية جارية — جاري جلب الوقت...", 'warn')
                time.sleep(3)
                real_cd = get_cooldown(uid, slot, perk_key)
                rt['cooldown'] = real_cd if (real_cd and real_cd > 0) else 60
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
            else:
                msg = str(result)[:80]
                add_log(uid, slot, f"❌ فشل: {msg}", 'error')
                if 'Token منتهي' in msg:
                    rt['status'] = 'error'; rt['enabled'] = False
                    socketio.emit('update', build_state(uid), room=f"user_{uid}"); break
                rt['cooldown'] = 60
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
                time.sleep(60)
        except Exception as e:
            add_log(uid, slot, f"💥 خطأ: {str(e)[:60]}", 'error')
            time.sleep(15)
    rt['status'] = 'stopped'; rt['enabled'] = False
    add_log(uid, slot, '⏹ البوت موقف', 'warn')

def fmt(s):
    s = int(s)
    if s < 60: return f'{s}s'
    if s < 3600: return f'{s//60}m {s%60:02d}s'
    return f'{s//3600}h {(s%3600)//60}m'

# ── Auth middleware ────────────────────────────────
def current_user():
    uid = session.get('user_id')
    if not uid: return None
    return db_fetchone("SELECT * FROM users WHERE id=%s AND is_active=1", (uid,))

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('is_admin'): return f(*args, **kwargs)
        return redirect('/login')
    return decorated

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user(): return f(*args, **kwargs)
        return redirect('/login')
    return decorated

# ── Routes: Auth ───────────────────────────────────
@app.route('/')
def index():
    if not current_user(): return redirect('/login')
    return Response(USER_HTML, mimetype='text/html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'GET':
        if current_user(): return redirect('/')
        return Response(LOGIN_HTML, mimetype='text/html')
    data = request.json or {}
    u = data.get('username','').strip()
    p = data.get('password','').strip()
    if u == ADMIN_USER and p == ADMIN_PASS:
        session.permanent = True
        session['is_admin'] = True
        session['username'] = u
        return jsonify({'ok': True, 'redirect': '/admin'})
    user = get_user(u)
    if user and user['is_active'] and user['password_hash'] == hash_pass(p):
        session.permanent = True
        session['user_id'] = user['id']
        session['username'] = u
        resp = jsonify({'ok': True, 'redirect': '/'})
        resp.set_cookie('username', u, max_age=86400*30)
        return resp
    return jsonify({'ok': False, 'error': 'اسم المستخدم أو كلمة السر غلط'}), 401

@app.route('/logout')
def logout():
    session.clear()
    resp = redirect('/login')
    resp.set_cookie('username', '', expires=0)
    return resp

# ── Routes: Admin ──────────────────────────────────
@app.route('/admin')
@admin_required
def admin_page():
    return Response(ADMIN_HTML, mimetype='text/html')

@app.route('/admin/api/users', methods=['GET'])
@admin_required
def admin_list_users():
    users = db_fetchall("SELECT id,username,created_at,is_active FROM users ORDER BY id DESC")
    return jsonify([dict(u) for u in users])

@app.route('/admin/api/users', methods=['POST'])
@admin_required
def admin_add_user():
    data = request.json or {}
    u = data.get('username','').strip()
    p = data.get('password','').strip()
    if not u or not p:
        return jsonify({'ok': False, 'error': 'اسم وكلمة سر مطلوبين'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO users (username, password_hash) VALUES (%s,%s) RETURNING id", (u, hash_pass(p)))
            uid = cur.fetchone()['id']
            for slot in [1,2]:
                cur.execute("INSERT INTO accounts (user_id, slot) VALUES (%s,%s) ON CONFLICT DO NOTHING", (uid, slot))
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'ok': False, 'error': 'الاسم موجود بالفعل'}), 400
        raise

@app.route('/admin/api/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(uid):
    db_exec("UPDATE users SET is_active = 1 - is_active WHERE id=%s", (uid,))
    return jsonify({'ok': True})

@app.route('/admin/api/users/<int:uid>/reset', methods=['POST'])
@admin_required
def admin_reset_pass(uid):
    data = request.json or {}
    p = data.get('password','').strip()
    if not p: return jsonify({'ok': False}), 400
    db_exec("UPDATE users SET password_hash=%s WHERE id=%s", (hash_pass(p), uid))
    return jsonify({'ok': True})

@app.route('/admin/api/users/<int:uid>/delete', methods=['POST'])
@admin_required
def admin_delete_user(uid):
    slots = db_fetchall("SELECT slot FROM accounts WHERE user_id=%s", (uid,))
    for s in slots:
        k = rt_key(uid, s['slot'])
        if k in stop_events: stop_events[k].set()
        runtime.pop(k, None); stop_events.pop(k, None); bot_threads.pop(k, None)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM accounts WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))
        conn.commit()
    return jsonify({'ok': True})

@app.route('/admin/api/users/<int:uid>/details', methods=['GET'])
@admin_required
def admin_user_details(uid):
    user = db_fetchone("SELECT id,username,created_at,is_active FROM users WHERE id=%s", (uid,))
    accs = db_fetchall("SELECT * FROM accounts WHERE user_id=%s ORDER BY slot", (uid,))
    if not user: return jsonify({'ok': False}), 404
    details = []
    for a in accs:
        rt = get_rt(uid, a['slot'])
        details.append({
            'slot': a['slot'], 'name': a['name'] or f'حساب {a["slot"]}', 'token': bool(a['token']),
            'perk': a['perk'], 'currency': a['currency'], 'balance': a['balance'], 'diamonds': a['diamonds'],
            'level': a['level_num'], 'upgrades': a['upgrades'], 'last_upgrade': a['last_upgrade'],
            'status': rt['status'], 'enabled': rt['enabled'],
        })
    return jsonify({'ok': True, 'user': dict(user), 'accounts': details})

# ── Routes: User API ───────────────────────────────
@app.route('/api/state')
@login_required
def api_state():
    u = current_user()
    return jsonify(build_state(u['id']))

@app.route('/api/start/<int:slot>', methods=['POST'])
@login_required
def api_start(slot):
    u = current_user()
    uid = u['id']
    get_accounts(uid)
    acc = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    if not acc: return jsonify({'error': 'not found'}), 404
    if not acc['token']: return jsonify({'error': 'أضف Token أولاً من الإعدادات'}), 400
    k = rt_key(uid, slot)
    if k in bot_threads and bot_threads[k].is_alive():
        return jsonify({'status': 'already running'})
    stop_events[k] = threading.Event()
    t = threading.Thread(target=bot_loop, args=(uid, slot, stop_events[k]), daemon=True)
    bot_threads[k] = t
    get_rt(uid, slot)['enabled'] = True
    t.start()
    return jsonify({'status': 'started'})

@app.route('/api/stop/<int:slot>', methods=['POST'])
@login_required
def api_stop(slot):
    u = current_user()
    uid = u['id']
    k = rt_key(uid, slot)
    if k in stop_events: stop_events[k].set()
    rt = get_rt(uid, slot)
    rt['enabled'] = False; rt['status'] = 'stopped'
    socketio.emit('update', build_state(uid), room=f"user_{uid}")
    return jsonify({'status': 'stopped'})

@app.route('/api/config/<int:slot>', methods=['POST'])
@login_required
def api_config(slot):
    u = current_user()
    uid = u['id']
    data = request.json or {}
    updates = {}
    if 'token' in data and data['token']: updates['token'] = data['token'].strip()
    if 'perk' in data and data['perk'] in PERKS: updates['perk'] = data['perk']
    if 'currency' in data and data['currency'] in ['money','diamond']: updates['currency'] = data['currency']
    if updates: save_account(uid, slot, **updates)
    if 'token' in updates:
        ok = refresh_profile(uid, slot)
        if not ok:
            return jsonify({'ok': False, 'error': 'Token خاطئ أو منتهي'}), 400
    socketio.emit('update', build_state(uid), room=f"user_{uid}")
    return jsonify({'ok': True})

@app.route('/api/refresh/<int:slot>', methods=['POST'])
@login_required
def api_refresh(slot):
    u = current_user()
    ok = refresh_profile(u['id'], slot)
    return jsonify({'ok': ok})

@app.route('/api/debug/<int:slot>')
@login_required
def api_debug(slot):
    u = current_user()
    uid = u['id']
    acc = db_fetchone("SELECT token FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
    if not acc or not acc['token']:
        return jsonify({'error': 'No token'})
    token = acc['token']
    result = {}
    result['players_skills'] = api_get(token, '/players/skills')
    r2 = api_get(token, '/players/profile')
    if r2:
        p = r2.get('player', r2)
        result['profile_skills'] = p.get('skills', {})
    for key in ['kisla','savas_teknikleri','bilim_insani']:
        result[f'skill_{key}'] = api_get(token, f'/players/skills/{key}')
    return jsonify(result)

# ========== GOOGLE OAuth2 FULL FLOW (مع redirect_uri ثابت) ==========
@app.route('/auth/google/full/<int:slot>')
@login_required
def google_full_auth(slot):
    session['oauth_slot'] = slot
    # استخدام عنوان ثابت HTTPS بدلاً من url_for الديناميكي
    redirect_uri = 'https://diplomacia-saas.onrender.com/auth/google/full/callback'
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/google/full/callback')
@login_required
def google_full_callback():
    import requests as req
    slot = session.pop('oauth_slot', 1)
    user_id = session['user_id']
    try:
        token = google.authorize_access_token()
        access_token = token.get('access_token')
        if not access_token:
            return "No access_token from Google", 400
        diplo_resp = req.post('https://diplomacia.com.tr/api/auth/google',
            json={'access_token': access_token},
            headers={
                'Content-Type': 'application/json',
                'Origin': 'https://diplomacia.com.tr',
                'Referer': 'https://diplomacia.com.tr/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/149.0.0.0'
            },
            timeout=15)
        if diplo_resp.status_code in (200, 201):
            diplo_data = diplo_resp.json()
            game_token = diplo_data.get('token')
            player = diplo_data.get('player', {})
            username = player.get('username', f'Slot {slot}')
            if game_token:
                db_exec(
                    "UPDATE accounts SET token=%s, name=%s WHERE user_id=%s AND slot=%s",
                    (game_token, username, user_id, slot)
                )
                threading.Thread(target=lambda: refresh_profile(user_id, slot), daemon=True).start()
                socketio.emit('update', build_state(user_id), room=f"user_{user_id}")
                return redirect('/#settings')
            else:
                return f"Game API returned no token: {diplo_data}", 400
        else:
            return f"Game API error {diplo_resp.status_code}: {diplo_resp.text}", 400
    except Exception as e:
        log.error(f"OAuth callback error: {e}")
        return f"Authentication error: {str(e)}", 500

# ── SocketIO ───────────────────────────────────────
@socketio.on('join')
def on_join():
    u = current_user()
    if u:
        join_room(f"user_{u['id']}")
        emit('update', build_state(u['id']))

@socketio.on('connect')
def on_connect():
    u = current_user()
    if u:
        join_room(f"user_{u['id']}")
        emit('update', build_state(u['id']))

# ── Scheduler ──────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: socketio.emit('ping', {}), 'interval', seconds=10)

def auto_refresh_stopped():
    try:
        users = db_fetchall("SELECT id FROM users WHERE is_active=1")
        for user in users:
            uid = user['id']
            for slot in [1,2]:
                acc = db_fetchone("SELECT * FROM accounts WHERE user_id=%s AND slot=%s", (uid, slot))
                if not acc or not acc['token']: continue
                rt = get_rt(uid, slot)
                if rt['enabled']: continue
                if rt.get('cd_checked'): continue
                perk = acc['perk']
                if perk not in PERKS: continue
                cd = get_cooldown(uid, slot, PERKS[perk]['key'])
                if cd is not None:
                    rt['cooldown'] = cd
                    rt['cd_checked'] = True
                    socketio.emit('update', build_state(uid), room=f"user_{uid}")
    except Exception as e:
        log.error(f"auto_refresh error: {e}")

scheduler.add_job(auto_refresh_stopped, 'interval', seconds=30)
scheduler.start()

# ── Main ───────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    log.info(f"🚀 SaaS Bot on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
