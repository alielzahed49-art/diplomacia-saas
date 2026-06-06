"""
Diplomacia Bot - SaaS Platform
أدمن بيصنع يوزرز، كل يوزر عنده حسابين وبيشغل البوت
"""
import os, json, time, threading, logging, hashlib, secrets
from datetime import datetime
from flask import Flask, jsonify, request, Response, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
from apscheduler.schedulers.background import BackgroundScheduler
import sqlite3

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'saas-diplo-2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

DB_PATH = os.environ.get('DB_PATH', '/tmp/saas.db')
BASE_URL = "https://diplomacia.com.tr/api"

PERKS = {
    'barracks':       {'label': 'BARRACKS',       'key': 'kisla'},
    'war_techniques': {'label': 'WAR TECHNIQUES', 'key': 'savas_teknikleri'},
    'scientist':      {'label': 'SCIENTIST',       'key': 'bilim_insani'},
}

ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'admin123')

# ── DB ─────────────────────────────────────────────
def get_db():
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            is_active INTEGER DEFAULT 1
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        db.commit()
    log.info("DB initialized")

init_db()

# ── Helpers ────────────────────────────────────────
def hash_pass(p): return hashlib.sha256(p.encode()).hexdigest()

def get_user(username):
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

def get_accounts(user_id):
    with get_db() as db:
        rows = db.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY slot", (user_id,)).fetchall()
        # ensure 2 slots exist
        existing = {r['slot'] for r in rows}
        for slot in [1, 2]:
            if slot not in existing:
                db.execute("INSERT OR IGNORE INTO accounts (user_id, slot) VALUES (?,?)", (user_id, slot))
        db.commit()
        return db.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY slot", (user_id,)).fetchall()

def save_account(user_id, slot, **kwargs):
    fields = ', '.join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id, slot]
    with get_db() as db:
        db.execute(f"UPDATE accounts SET {fields} WHERE user_id=? AND slot=?", vals)
        db.commit()

# ── Runtime state (in-memory) ──────────────────────
# { "uid_slot" : { status, cooldown, enabled, logs[] } }
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
    with get_db() as db:
        acc = db.execute("SELECT * FROM accounts WHERE user_id=? AND slot=?", (uid, slot)).fetchone()
    if not acc or not acc['token']: return False
    data = api_get(acc['token'], '/players/profile')
    if not data: return False
    try:
        p = data.get('player', data)
        skills = p.get('skills', {})
        lp = p.get('levelProgress', {})
        pct = lp.get('percentage', 0)
        xp = round(pct * 100) if isinstance(pct, float) and pct <= 1 else round(pct)
        save_account(uid, slot,
            name=p.get('username', acc['name'] or f'حساب {slot}'),
            balance=f"${p.get('balance',0):,}",
            diamonds=str(p.get('diamonds',0)),
            level_num=str(p.get('level','?')),
            xp_pct=xp,
            lv_barracks=str(skills.get('kisla','?')),
            lv_war=str(skills.get('savas_teknikleri','?')),
            lv_scientist=str(skills.get('bilim_insani','?')),
        )
        socketio.emit('update', build_state(uid), room=f"user_{uid}")
        return True
    except Exception as e:
        log.error(f"Profile parse err: {e}")
        return False

def get_cooldown(uid, slot, perk_key):
    with get_db() as db:
        acc = db.execute("SELECT token FROM accounts WHERE user_id=? AND slot=?", (uid, slot)).fetchone()
    if not acc: return None
    token = acc['token']

    data = api_get(token, '/players/profile')
    if not data: return None
    try:
        p = data.get('player', data)
        skills = p.get('skills', {})
        val = skills.get(perk_key)
        if val is None: return 0
        if isinstance(val, (int, float)): return 0
        if isinstance(val, dict):
            for f in ['cooldown_remaining','remaining_seconds','cooldown','remaining']:
                cd = val.get(f, 0)
                if cd and int(cd) > 0: return int(cd)
            for f in ['upgrade_end_time','upgrading_until']:
                et = val.get(f)
                if et:
                    r = int(et) - int(time.time())
                    if r > 0: return r
            if val.get('is_upgrading') or val.get('upgrading'): return 65
        return 0
    except: return 0

def do_upgrade(uid, slot, perk_key, currency):
    with get_db() as db:
        acc = db.execute("SELECT token FROM accounts WHERE user_id=? AND slot=?", (uid, slot)).fetchone()
    if not acc: return False, 'no account'
    token = acc['token']
    payload = {'skill': perk_key, 'type': currency}
    status, resp = api_post(token, '/players/skills/upgrade', payload)
    log.info(f"Upgrade U{uid}/S{slot} {payload}: {status} {str(resp)[:100]}")
    if status in (200, 201): return True, resp
    if status == 401: return False, 'Token منتهي الصلاحية'
    msg = resp.get('message', str(resp)) if isinstance(resp, dict) else str(resp)
    return False, msg

def bot_loop(uid, slot, stop_ev):
    with get_db() as db:
        acc = db.execute("SELECT * FROM accounts WHERE user_id=? AND slot=?", (uid, slot)).fetchone()
    if not acc: return
    perk = acc['perk']
    perk_key = PERKS[perk]['key']
    perk_label = PERKS[perk]['label']
    currency = acc['currency']
    rt = get_rt(uid, slot)
    rt['status'] = 'running'
    rt['enabled'] = True

    add_log(uid, slot, f"▶ البوت شغّال — {perk_label}", 'ok')

    if refresh_profile(uid, slot):
        with get_db() as db:
            a = db.execute("SELECT * FROM accounts WHERE user_id=? AND slot=?", (uid, slot)).fetchone()
        add_log(uid, slot, f"✅ متصل — {a['name']} | {a['balance']} | 💎{a['diamonds']}", 'ok')
    else:
        add_log(uid, slot, '⚠️ Token منتهي أو خاطئ', 'warn')
        rt['status'] = 'error'; rt['enabled'] = False
        socketio.emit('update', build_state(uid), room=f"user_{uid}")
        return

    fail_count = 0
    while not stop_ev.is_set():
        try:
            cd = get_cooldown(uid, slot, perk_key)
            if cd is None:
                fail_count += 1
                if fail_count >= 5:
                    add_log(uid, slot, '❌ فشل 5 مرات — توقف', 'error'); break
                time.sleep(30); continue
            fail_count = 0

            if cd > 0:
                rt['cooldown'] = cd
                add_log(uid, slot, f"⏳ {perk_label} — كمل {fmt(cd)}", 'warn')
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
                waited = 0
                while waited < cd and not stop_ev.is_set():
                    time.sleep(1); waited += 1
                    if rt['cooldown'] > 0: rt['cooldown'] -= 1
                continue

            rt['cooldown'] = 0
            add_log(uid, slot, f"⚡ {perk_label} جاهز — جاري الترقية...", 'ok')
            success, result = do_upgrade(uid, slot, perk_key, currency)

            if success:
                with get_db() as db:
                    db.execute("UPDATE accounts SET upgrades=upgrades+1, last_upgrade=? WHERE user_id=? AND slot=?",
                               (datetime.now().strftime('%H:%M:%S'), uid, slot))
                    db.commit()
                rt['cooldown'] = 65
                add_log(uid, slot, f"✅ تمت الترقية!", 'ok')
                refresh_profile(uid, slot)
            else:
                msg = str(result)[:80]
                add_log(uid, slot, f"❌ فشل: {msg}", 'error')
                if 'Token منتهي' in msg:
                    rt['status'] = 'error'; rt['enabled'] = False
                    socketio.emit('update', build_state(uid), room=f"user_{uid}"); break
                rt['cooldown'] = 65
                socketio.emit('update', build_state(uid), room=f"user_{uid}")
                for _ in range(65):
                    if stop_ev.is_set(): break
                    time.sleep(1)
                    if rt['cooldown'] > 0: rt['cooldown'] -= 1

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

# ── HTML ───────────────────────────────────────────
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Panel</title>
<style>
:root{--gold:#c8a84b;--bg:#07071a;--card:#0f0f28;--panel:#161635;--border:rgba(200,168,75,.18);--green:#4caf72;--red:#e94560;--text:#d0d0e8;--muted:#505078}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:1.5rem}
h1{color:var(--gold);font-size:1.3rem;margin-bottom:1.5rem;letter-spacing:2px}
h2{font-size:.85rem;color:rgba(200,168,75,.7);letter-spacing:2px;margin-bottom:.8rem;text-transform:uppercase}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:1.2rem;margin-bottom:1rem}
.inp{width:100%;padding:8px 10px;background:var(--panel);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;outline:none;margin-bottom:8px}
.inp:focus{border-color:var(--gold)}
.btn{padding:8px 18px;border:none;border-radius:6px;font-weight:700;font-size:12px;cursor:pointer}
.btn-g{background:var(--gold);color:#07071a}
.btn-r{background:var(--red);color:#fff}
.btn-sm{padding:5px 12px;font-size:11px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--muted);padding:6px 10px;text-align:right;font-weight:600;border-bottom:1px solid var(--border)}
td{padding:8px 10px;border-bottom:1px solid rgba(200,168,75,.06);vertical-align:middle}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700}
.tag-on{background:rgba(76,175,114,.15);color:var(--green)}
.tag-off{background:rgba(80,80,120,.2);color:var(--muted)}
.msg{margin-top:8px;font-size:12px;padding:7px;border-radius:5px;display:none}
.msg.ok{background:rgba(76,175,114,.15);color:var(--green);display:block}
.msg.err{background:rgba(233,69,96,.12);color:var(--red);display:block}
</style>
</head>
<body>
<h1>⚔ ADMIN PANEL</h1>

<div class="card">
  <h2>إضافة يوزر جديد</h2>
  <input class="inp" id="new-user" placeholder="اسم المستخدم">
  <input class="inp" id="new-pass" type="password" placeholder="كلمة السر">
  <button class="btn btn-g" onclick="addUser()">➕ إضافة</button>
  <div id="add-msg" class="msg"></div>
</div>

<div class="card">
  <h2>المستخدمين</h2>
  <table>
    <thead><tr><th>#</th><th>اسم المستخدم</th><th>تاريخ الإنشاء</th><th>الحالة</th><th>إجراء</th></tr></thead>
    <tbody id="users-table"><tr><td colspan="5" style="color:var(--muted);text-align:center">جاري التحميل...</td></tr></tbody>
  </table>
</div>

<script>
async function loadUsers() {
  const r = await fetch('/admin/api/users');
  const users = await r.json();
  const tbody = document.getElementById('users-table');
  if (!users.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted);text-align:center">لا يوجد مستخدمين</td></tr>';
    return;
  }
  tbody.innerHTML = users.map(u => `
    <tr>
      <td>${u.id}</td>
      <td style="color:var(--gold);font-weight:700">${u.username}</td>
      <td style="color:var(--muted)">${u.created_at}</td>
      <td><span class="tag ${u.is_active ? 'tag-on':'tag-off'}">${u.is_active ? 'نشط':'موقف'}</span></td>
      <td>
        <button class="btn btn-sm ${u.is_active ? 'btn-r':'btn-g'}" onclick="toggleUser(${u.id},${u.is_active})">
          ${u.is_active ? '⏸ إيقاف':'▶ تفعيل'}
        </button>
        <button class="btn btn-sm btn-r" style="margin-right:5px" onclick="resetPass(${u.id})">🔑 ريسيت</button>
      </td>
    </tr>`).join('');
}

async function addUser() {
  const u = document.getElementById('new-user').value.trim();
  const p = document.getElementById('new-pass').value.trim();
  const msg = document.getElementById('add-msg');
  if (!u || !p) { showMsg(msg,'اكتب اسم وكلمة سر','err'); return; }
  const r = await fetch('/admin/api/users', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username:u, password:p})
  });
  const d = await r.json();
  if (d.ok) {
    showMsg(msg, `✅ تم إضافة "${u}" بنجاح!`, 'ok');
    document.getElementById('new-user').value = '';
    document.getElementById('new-pass').value = '';
    loadUsers();
  } else { showMsg(msg, '❌ ' + (d.error||'خطأ'), 'err'); }
}

async function toggleUser(id, active) {
  await fetch(`/admin/api/users/${id}/toggle`, {method:'POST'});
  loadUsers();
}

async function resetPass(id) {
  const p = prompt('كلمة السر الجديدة:');
  if (!p) return;
  const r = await fetch(`/admin/api/users/${id}/reset`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password:p})
  });
  const d = await r.json();
  alert(d.ok ? '✅ تم تغيير كلمة السر' : '❌ خطأ');
}

function showMsg(el, txt, cls) {
  el.textContent = txt; el.className = 'msg ' + cls;
  setTimeout(() => el.className = 'msg', 4000);
}

loadUsers();
</script>
</body>
</html>"""

USER_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diplomacia Bot</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
:root{--gold:#c8a84b;--bg:#07071a;--card:#0f0f28;--panel:#161635;--border:rgba(200,168,75,.18);--green:#4caf72;--red:#e94560;--blue:#4a9eff;--text:#d0d0e8;--muted:#505078}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
header{background:rgba(7,7,26,.97);border-bottom:1px solid var(--border);padding:0 1.2rem;height:54px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.logo{color:var(--gold);font-weight:700;font-size:1rem;letter-spacing:2px}
.logout{padding:5px 12px;border:1px solid var(--border);border-radius:5px;background:none;color:var(--muted);font-size:11px;cursor:pointer}
.logout:hover{border-color:var(--red);color:var(--red)}
.main{max-width:900px;margin:0 auto;padding:1.2rem;padding-bottom:80px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.2rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:border-color .2s}
.card.running{border-color:rgba(76,175,114,.4)}
.card.error{border-color:rgba(233,69,96,.4)}
.ch{background:var(--panel);padding:.9rem 1rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.av{width:36px;height:36px;border-radius:50%;background:#1a1a40;border:1.5px solid var(--gold);display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.cn{font-weight:700;font-size:13px;color:var(--gold)}
.cs{font-size:10px;color:var(--muted);margin-top:2px}
.badge{margin-right:auto;padding:2px 9px;border-radius:20px;font-size:10px;font-weight:700}
.b-run{background:rgba(76,175,114,.15);color:var(--green);border:1px solid rgba(76,175,114,.3)}
.b-stop{background:rgba(80,80,120,.2);color:var(--muted);border:1px solid var(--border)}
.b-err{background:rgba(233,69,96,.12);color:var(--red);border:1px solid rgba(233,69,96,.3)}
.cb{padding:.9rem 1rem}
.res{display:flex;gap:8px;margin-bottom:.8rem}
.rc{flex:1;background:var(--panel);border-radius:6px;padding:5px 8px;font-size:11px}
.rc span{color:var(--gold);font-weight:700}
.xb{height:3px;background:var(--panel);border-radius:2px;margin-bottom:.8rem;overflow:hidden}
.xf{height:100%;background:var(--gold);border-radius:2px;transition:width 1s}
.slbl{font-size:10px;color:var(--muted);letter-spacing:1.5px;margin-bottom:5px}
.prks{display:flex;flex-direction:column;gap:5px;margin-bottom:.8rem}
.pr{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;background:var(--panel);cursor:pointer;border:1px solid transparent;transition:all .15s}
.pr:hover{background:#1e1e45}
.pr.sel{border-color:rgba(200,168,75,.4);background:rgba(200,168,75,.07)}
.pi{width:24px;height:24px;border-radius:5px;background:rgba(200,168,75,.1);display:flex;align-items:center;justify-content:center;font-size:12px;flex-shrink:0}
.pn{font-size:11px;font-weight:600}
.pd{font-size:10px;color:var(--muted)}
.pl{margin-right:auto;font-size:10px;color:var(--gold);background:rgba(200,168,75,.1);padding:2px 7px;border-radius:4px}
.pcd{font-size:10px;color:var(--muted);min-width:40px;text-align:center}
.pcd.rdy{color:var(--green);font-weight:700}
.pcd.upg{color:var(--blue)}
.cur{display:flex;gap:6px;margin-bottom:.8rem}
.cb2{flex:1;padding:5px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--muted);font-size:11px;cursor:pointer;transition:all .15s;text-align:center}
.cb2.act{border-color:var(--gold);color:var(--gold);background:rgba(200,168,75,.1)}
.cd-big{text-align:center;font-size:2rem;font-weight:700;color:var(--green);letter-spacing:3px;margin:.5rem 0;min-height:48px}
.cd-big.wait{color:var(--gold)}
.ctrl{display:flex;gap:7px;padding:.8rem 1rem;border-top:1px solid var(--border)}
.btn{flex:1;padding:8px;border:1px solid var(--border);border-radius:7px;background:transparent;color:var(--text);font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:5px}
.btn:hover{background:#1e1e45}
.btn-s{background:rgba(76,175,114,.12);border-color:rgba(76,175,114,.4);color:var(--green)}
.btn-s:hover{background:rgba(76,175,114,.22)}
.btn-x{background:rgba(233,69,96,.1);border-color:rgba(233,69,96,.35);color:var(--red)}
.btn-x:hover{background:rgba(233,69,96,.2)}
.btn-g{background:rgba(200,168,75,.1);border-color:rgba(200,168,75,.4);color:var(--gold)}
.inp{width:100%;padding:8px 10px;background:var(--panel);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;outline:none;transition:border-color .15s;margin-bottom:7px}
.inp:focus{border-color:var(--gold)}
.log-panel{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:1rem}
.lh{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;border-bottom:1px solid var(--border);background:var(--panel)}
.lt{font-size:.7rem;color:rgba(200,168,75,.7);letter-spacing:2px}
.lb{background:none;border:none;color:var(--muted);font-size:11px;cursor:pointer}
.lb:hover{color:var(--red)}
.log-body{padding:.7rem 1rem;max-height:200px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.9}
.ll{display:flex;gap:8px}
.lt2{color:var(--muted);flex-shrink:0}
.la{color:rgba(200,168,75,.6);flex-shrink:0;min-width:75px}
.lm{color:var(--text)}
.ll.ok .lm{color:var(--green)}
.ll.warn .lm{color:var(--gold)}
.ll.error .lm{color:var(--red)}
.ll.info .lm{color:var(--blue)}
.tok-section{padding:.8rem 1rem;border-top:1px solid var(--border)}
.tok-status{font-size:11px;padding:4px 8px;border-radius:4px;margin-bottom:8px;display:none}
.tok-status.ok{background:rgba(76,175,114,.15);color:var(--green);display:block}
.tok-status.err{background:rgba(233,69,96,.12);color:var(--red);display:block}
.bnav{position:fixed;bottom:0;left:0;right:0;background:rgba(7,7,26,.98);border-top:1px solid var(--border);display:flex}
.ni{flex:1;display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:3px;font-size:9px;letter-spacing:1px;color:var(--muted);cursor:pointer;border:none;background:none;font-family:inherit;transition:color .15s}
.ni.act{color:var(--gold)}
.ni-icon{font-size:18px}
.page{display:none}
.page.act{display:block}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">⚔ DIPLOMACIA BOT</div>
  <div style="display:flex;align-items:center;gap:10px">
    <span id="user-label" style="font-size:11px;color:var(--muted)"></span>
    <button class="logout" onclick="location.href='/logout'">خروج</button>
  </div>
</header>

<div class="main">
<div id="page-home" class="page act">
  <div class="grid" id="acc-grid"></div>
  <div style="font-size:.7rem;color:rgba(200,168,75,.6);letter-spacing:2px;margin-bottom:.7rem">السجل</div>
  <div class="log-panel">
    <div class="lh"><span class="lt">ACTIVITY LOG</span><button class="lb" onclick="clearLog()">مسح</button></div>
    <div class="log-body" id="log-body">
      <div class="ll info"><span class="lt2">--:--</span><span class="la">[SYSTEM]</span><span class="lm">البوت جاهز</span></div>
    </div>
  </div>
</div>

<div id="page-settings" class="page">
  <div style="font-size:.7rem;color:rgba(200,168,75,.6);letter-spacing:2px;margin-bottom:.7rem">إضافة Token</div>
  <div class="log-panel">
    <div style="padding:.9rem 1rem">
      <div style="font-size:12px;color:var(--muted);margin-bottom:.5rem">حساب 1</div>
      <div class="tok-status" id="ts1"></div>
      <input class="inp" id="tok1" placeholder="Token الحساب الأول (eyJhbG...)">
      <button class="btn btn-g" style="width:100%;margin-bottom:1rem" onclick="saveToken(1)">💾 حفظ Token حساب 1</button>
      <div style="font-size:12px;color:var(--muted);margin-bottom:.5rem">حساب 2</div>
      <div class="tok-status" id="ts2"></div>
      <input class="inp" id="tok2" placeholder="Token الحساب الثاني (eyJhbG...)">
      <button class="btn btn-g" style="width:100%;margin-bottom:1rem" onclick="saveToken(2)">💾 حفظ Token حساب 2</button>
      <hr style="border-color:var(--border);margin:.5rem 0">
      <div style="font-size:11px;color:var(--muted);line-height:2.2">
        <div>1️⃣ افتح diplomacia.com.tr</div>
        <div>2️⃣ F12 → Network → اعمل أي action</div>
        <div>3️⃣ دور على <b style="color:var(--gold)">Authorization: Bearer</b></div>
        <div>4️⃣ انسخ الـ token بعد Bearer</div>
        <div>⏱ Token بيخلص كل ~7 أيام</div>
      </div>
    </div>
  </div>
</div>
</div>

<nav class="bnav">
  <button class="ni act" id="nav-home" onclick="switchPage('home',this)"><span class="ni-icon">⚔</span>الرئيسية</button>
  <button class="ni" id="nav-settings" onclick="switchPage('settings',this)"><span class="ni-icon">⚙</span>الإعدادات</button>
</nav>

<script>
const PERKS = {
  barracks:       {label:'BARRACKS',       icon:'🏰', desc:'+Military Power'},
  war_techniques: {label:'WAR TECHNIQUES', icon:'⚔',  desc:'+War Damage'},
  scientist:      {label:'SCIENTIST',      icon:'🔬', desc:'+Factory Income'},
};

const socket = io();
let state = {};

document.getElementById('user-label').textContent = document.cookie.match(/username=([^;]+)/)?.[1] || '';

socket.on('connect', () => {
  socket.emit('join');
});
socket.on('update', s => { state = s; renderAll(); });
socket.on('log', e => addLogEntry(e.slot, e.entry));

function renderAll() {
  const grid = document.getElementById('acc-grid');
  grid.innerHTML = ['1','2'].map(id => renderCard(id, state[id])).join('');
}

function renderCard(id, acc) {
  if (!acc) return '';
  const xpPct = acc.xp_pct || 0;
  const stClass = acc.enabled ? 'running' : acc.status === 'error' ? 'error' : '';
  const badge = acc.enabled
    ? `<span class="badge b-run">نشط</span>`
    : acc.status === 'error'
    ? `<span class="badge b-err">خطأ</span>`
    : `<span class="badge b-stop">موقف</span>`;

  const perksHtml = Object.entries(PERKS).map(([key, p]) => {
    const isSel = acc.perk === key;
    const lvl = acc.level?.[key] || '?';
    let cdHtml = `<span class="pcd rdy">جاهز ✓</span>`;
    if (isSel && acc.enabled && acc.cooldown > 0)
      cdHtml = `<span class="pcd upg">${fmt(acc.cooldown)}</span>`;
    return `<div class="pr ${isSel?'sel':''}" onclick="selPerk('${id}','${key}')">
      <div class="pi">${p.icon}</div>
      <div><div class="pn">${p.label}</div><div class="pd">${p.desc}</div></div>
      <div class="pl">Lv.${lvl}</div>${cdHtml}</div>`;
  }).join('');

  const cdText = acc.enabled && acc.cooldown > 0
    ? `<div class="cd-big wait">${fmtCd(acc.cooldown)}</div>`
    : acc.enabled ? `<div class="cd-big">⚡ جاهز</div>`
    : `<div class="cd-big" style="font-size:1rem;color:var(--muted)">موقف</div>`;

  return `<div class="card ${stClass}">
    <div class="ch">
      <div class="av">🎮</div>
      <div>
        <div class="cn">${acc.name} ${acc.token?'✅':'❌'} <span style="font-size:10px;color:var(--muted)">${acc.level_num!=='?'?'Lv.'+acc.level_num:''}</span></div>
        <div class="cs">ترقيات: ${acc.upgrades} | آخر: ${acc.last_upgrade}</div>
      </div>${badge}
    </div>
    <div class="cb">
      <div class="res">
        <div class="rc">💵 <span>${acc.balance}</span></div>
        <div class="rc">💎 <span>${acc.diamonds}</span></div>
        <div class="rc">🔰 <span>${xpPct}%</span></div>
      </div>
      <div class="xb"><div class="xf" style="width:${xpPct}%"></div></div>
      <div class="slbl">العملة</div>
      <div class="cur">
        <button class="cb2 ${acc.currency==='money'?'act':''}" onclick="selCur('${id}','money')">💵 Money</button>
        <button class="cb2 ${acc.currency==='diamond'?'act':''}" onclick="selCur('${id}','diamond')">💎 Diamond</button>
      </div>
      <div class="slbl">اختر البيرك</div>
      <div class="prks">${perksHtml}</div>
      ${cdText}
    </div>
    <div class="ctrl">
      ${acc.enabled
        ? `<button class="btn btn-x" onclick="stopAcc('${id}')">⏹ إيقاف</button>`
        : `<button class="btn btn-s" onclick="startAcc('${id}')">▶ تشغيل</button>`}
      <button class="btn btn-g" onclick="switchPage('settings',document.getElementById('nav-settings'))">🔑 Token</button>
      <button class="btn" onclick="refreshAcc('${id}')">🔄</button>
    </div>
  </div>`;
}

async function startAcc(slot) {
  const r = await fetch(`/api/start/${slot}`, {method:'POST'});
  const d = await r.json();
  if (d.error) alert(d.error);
}
async function stopAcc(slot) { await fetch(`/api/stop/${slot}`, {method:'POST'}); }
async function selPerk(slot, perk) {
  await fetch(`/api/config/${slot}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({perk})});
}
async function selCur(slot, currency) {
  await fetch(`/api/config/${slot}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({currency})});
}
async function saveToken(slot) {
  const tok = document.getElementById(`tok${slot}`).value.trim();
  if (!tok) { alert('الصق الـ Token أولاً'); return; }
  const r = await fetch(`/api/config/${slot}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: tok})
  });
  const el = document.getElementById(`ts${slot}`);
  if (r.ok) {
    el.textContent = '✅ تم حفظ الـ Token'; el.className = 'tok-status ok';
    document.getElementById(`tok${slot}`).value = '';
    setTimeout(() => el.className = 'tok-status', 3000);
  } else { el.textContent = '❌ خطأ'; el.className = 'tok-status err'; }
}
async function refreshAcc(slot) { await fetch(`/api/refresh/${slot}`, {method:'POST'}); }

function addLogEntry(slot, e) {
  const body = document.getElementById('log-body');
  const div = document.createElement('div');
  div.className = `ll ${e.level}`;
  div.innerHTML = `<span class="lt2">${e.time}</span><span class="la">[حساب ${slot}]</span><span class="lm">${e.msg}</span>`;
  body.insertBefore(div, body.firstChild);
  if (body.children.length > 80) body.lastChild.remove();
}
function clearLog() { document.getElementById('log-body').innerHTML = ''; }
function switchPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('act'));
  document.getElementById('page-' + name).classList.add('act');
  document.querySelectorAll('.ni').forEach(n => n.classList.remove('act'));
  btn.classList.add('act');
}
function fmt(s) {
  s = Math.floor(s);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + String(s%60).padStart(2,'0') + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtCd(s) {
  s = Math.floor(s);
  return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
}

fetch('/api/state').then(r=>r.json()).then(s => { state = s; renderAll(); });
setInterval(() => {
  Object.keys(state).forEach(id => {
    if (state[id].enabled && state[id].cooldown > 0) state[id].cooldown = Math.max(0, state[id].cooldown - 1);
  });
  renderAll();
}, 1000);
</script>
</body>
</html>"""

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>تسجيل الدخول</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#07071a;color:#d0d0e8;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#0f0f28;border:1px solid rgba(200,168,75,.18);border-radius:14px;padding:2rem;width:320px}
h1{color:#c8a84b;font-size:1.1rem;letter-spacing:2px;text-align:center;margin-bottom:1.5rem}
.inp{width:100%;padding:10px 12px;background:#161635;border:1px solid rgba(200,168,75,.18);border-radius:7px;color:#d0d0e8;font-size:13px;outline:none;margin-bottom:10px}
.inp:focus{border-color:#c8a84b}
.btn{width:100%;padding:10px;background:#c8a84b;border:none;border-radius:7px;color:#07071a;font-weight:700;font-size:14px;cursor:pointer;margin-top:4px}
.btn:hover{background:#d4b85a}
.err{color:#e94560;font-size:12px;text-align:center;margin-top:8px;display:none}
.err.show{display:block}
</style>
</head>
<body>
<div class="box">
  <h1>⚔ DIPLOMACIA BOT</h1>
  <input class="inp" id="u" placeholder="اسم المستخدم" autofocus>
  <input class="inp" id="p" type="password" placeholder="كلمة السر" onkeydown="if(event.key==='Enter')login()">
  <button class="btn" onclick="login()">دخول</button>
  <div class="err" id="err">اسم المستخدم أو كلمة السر غلط</div>
</div>
<script>
async function login() {
  const u = document.getElementById('u').value.trim();
  const p = document.getElementById('p').value.trim();
  const r = await fetch('/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username:u, password:p})
  });
  const d = await r.json();
  if (d.ok) location.href = d.redirect || '/';
  else {
    const e = document.getElementById('err');
    e.textContent = d.error || 'خطأ في تسجيل الدخول';
    e.classList.add('show');
  }
}
</script>
</body>
</html>"""

# ── Auth middleware ────────────────────────────────
def current_user():
    uid = session.get('user_id')
    if not uid: return None
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()

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
    # Admin check
    if u == ADMIN_USER and p == ADMIN_PASS:
        session['is_admin'] = True
        session['username'] = u
        return jsonify({'ok': True, 'redirect': '/admin'})
    # User check
    user = get_user(u)
    if user and user['is_active'] and user['password_hash'] == hash_pass(p):
        session['user_id'] = user['id']
        session['username'] = u
        # set cookie for display
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
    with get_db() as db:
        users = db.execute("SELECT id,username,created_at,is_active FROM users ORDER BY id DESC").fetchall()
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
        with get_db() as db:
            db.execute("INSERT INTO users (username, password_hash) VALUES (?,?)", (u, hash_pass(p)))
            uid = db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()['id']
            for slot in [1, 2]:
                db.execute("INSERT OR IGNORE INTO accounts (user_id, slot) VALUES (?,?)", (uid, slot))
            db.commit()
        return jsonify({'ok': True})
    except sqlite3.IntegrityError:
        return jsonify({'ok': False, 'error': 'الاسم موجود بالفعل'}), 400

@app.route('/admin/api/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(uid):
    with get_db() as db:
        db.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (uid,))
        db.commit()
    return jsonify({'ok': True})

@app.route('/admin/api/users/<int:uid>/reset', methods=['POST'])
@admin_required
def admin_reset_pass(uid):
    data = request.json or {}
    p = data.get('password','').strip()
    if not p: return jsonify({'ok': False}), 400
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pass(p), uid))
        db.commit()
    return jsonify({'ok': True})

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
    get_accounts(uid)  # ensure slots exist
    with get_db() as db:
        acc = db.execute("SELECT * FROM accounts WHERE user_id=? AND slot=?", (uid, slot)).fetchone()
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
    # refresh after token save
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
scheduler.start()

# ── Main ───────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    log.info(f"🚀 SaaS Bot on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
