import json
import os
import sqlite3
import secrets
import uuid
from datetime import date as date_cls, datetime, timezone, timedelta
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g
)
from werkzeug.utils import secure_filename

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _writable_path(candidates):
    for path in candidates:
        if not path:
            continue
        folder = os.path.dirname(path) or "."
        # Nooit zelf /var/data aanmaken: dat moet de Render-disk zijn.
        if folder == "/var/data" and not os.path.isdir(folder):
            continue
        try:
            if folder not in (".", ""):
                os.makedirs(folder, exist_ok=True)
            with open(path, "a"):
                pass
            return path
        except OSError:
            continue
    return [p for p in candidates if p][-1]


DB_PATH = _writable_path([
    os.environ.get("DATABASE_PATH"),
    "/var/data/busportaal.db",
    "/tmp/busportaal.db",
    os.path.join(APP_DIR, "data", "app.db"),
])
if str(DB_PATH).startswith("/var/data"):
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or "/var/data/uploads"
elif str(DB_PATH).startswith("/tmp"):
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or "/tmp/busportaal-uploads"
else:
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(APP_DIR, "static", "uploads")
ALLOWED_PHOTOS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
INSPECTIE_URL = "https://huisconcierge.nl/inspectie"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "lokale-test-sleutel-wijzig-later")
app.config["PREFERRED_URL_SCHEME"] = "https"
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

AGENDA_NAMES = [
    "Bus 2 Huisconcierge | Shiela",
    "Bus 3 Huisconcierge",
    "huisconcierge",
]


def official_agenda_name(*parts):
    blob = " ".join(str(p) for p in parts if p).strip()
    low = blob.lower()
    for name in sorted(AGENDA_NAMES, key=len, reverse=True):
        if name.lower() in low:
            return name
    return blob or "Onbekende agenda"


USERS = [
    ("admin", "admin123", "admin", "Beheerder"),
    ("jan", "jan123", "zzper", "Jan de Vries"),
    ("piet", "piet123", "zzper", "Piet Bakker"),
    ("lisa", "lisa123", "zzper", "Lisa Hendriks"),
]

# Gebaseerd op echte Teamleader-export Bus 3 Huisconcierge 26-08-2026
SAMPLE_JOBS = [
    {
        "time": "08:00 - 10:00",
        "type": "Afspraak",
        "title": "Voorbeeldrit Bus 1",
        "details": "Testdata tot Teamleader per agenda werkt.",
        "bus": "huisconcierge",
        "extern": "Voorbeeldklant",
        "phone": "",
        "location_name": "",
        "address": "Merwedeweg 5C1, 3621 LP Breukelen",
        "amount": "",
    },
    {
        "time": "08:30 - 11:00",
        "type": "Afspraak",
        "title": "Voorbeeldrit Bus 2",
        "details": "Testdata tot Teamleader per agenda werkt.",
        "bus": "Bus 2 Huisconcierge | Shiela",
        "extern": "Voorbeeldklant",
        "phone": "",
        "location_name": "",
        "address": "Merwedeweg 5C1, 3621 LP Breukelen",
        "amount": "",
    },
    {
        "time": "06:15 - 07:15",
        "type": "Taak",
        "title": "2 man - 2 man",
        "details": "",
        "bus": "Bus 3 Huisconcierge",
        "extern": "",
        "phone": "",
        "location_name": "",
        "address": "",
        "amount": "",
    },
    {
        "time": "09:00 - 11:45",
        "type": "Afspraak",
        "title": "Lelystad SBB",
        "details": "- kantine\n- camera masten\n- camper\n- tribune",
        "bus": "Bus 3 Huisconcierge",
        "extern": "Fred Vowinkel",
        "phone": "+31651170329",
        "location_name": "Fred Vowinkel",
        "address": "Talingweg 89, 8218 NX Lelystad",
        "amount": "",
    },
    {
        "time": "12:15 - 15:15",
        "type": "Afspraak",
        "title": "Lelystad SB VVE",
        "details": "Huisnummers 183, 185, 187, 189\nMelden bij huisnummer 185",
        "bus": "Bus 3 Huisconcierge",
        "extern": "Willem Vroom VVE De Raadgever",
        "phone": "+31620433417",
        "location_name": "Willem Vroom VVE De Raadgever",
        "address": "Saerdam 185, 8242 JD Lelystad",
        "amount": "EUR 519",
    },
    {
        "time": "15:45 - 17:15",
        "type": "Afspraak",
        "title": "Lelystad Schoonzicht",
        "details": "Ramen wassen entree. Enzo weet welke ramen.",
        "bus": "Bus 3 Huisconcierge",
        "extern": "",
        "phone": "",
        "location_name": "Kantoor Breukelen",
        "address": "Merwedeweg 5C1, 3621 LP Breukelen",
        "amount": "",
    },
]


def maps_links(address):
    if not address:
        return "", ""
    q = quote_plus(address)
    return (
        f"https://www.google.com/maps/search/?api=1&query={q}",
        f"https://waze.com/ul?q={q}&navigate=yes",
    )


def get_db():
    if "db" not in g:
        folder = os.path.dirname(DB_PATH) or "."
        os.makedirs(folder, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)").fetchall()] if db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone() else []
    if cols and "title" not in cols:
        db.execute("DROP TABLE jobs")
        db.commit()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT,
            name TEXT
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            type TEXT,
            title TEXT,
            details TEXT,
            bus TEXT,
            extern TEXT,
            phone TEXT,
            location_name TEXT,
            address TEXT,
            amount TEXT,
            zzper_id INTEGER,
            payment TEXT,
            comment TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS hidden_jobs (
            date TEXT,
            time TEXT,
            title TEXT,
            bus TEXT,
            PRIMARY KEY (date, time, title, bus)
        );
        CREATE TABLE IF NOT EXISTS agenda_zzpers (
            date TEXT,
            bus TEXT,
            zzper_id INTEGER,
            PRIMARY KEY (date, bus, zzper_id)
        );
        CREATE TABLE IF NOT EXISTS week_roster (
            date TEXT,
            zzper_id INTEGER,
            PRIMARY KEY (date, zzper_id)
        );
        CREATE TABLE IF NOT EXISTS job_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            filename TEXT
        );
        CREATE TABLE IF NOT EXISTS dakgoot_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER UNIQUE,
            status TEXT,
            comment TEXT,
            address TEXT,
            created_at TEXT,
            zzper_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS report_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER,
            filename TEXT
        );
        CREATE TABLE IF NOT EXISTS bijverkoop (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            zzper_id INTEGER,
            omschrijving TEXT,
            bedrag REAL,
            bonus REAL DEFAULT 0,
            created_at TEXT,
            date TEXT
        );
        """
    )
    for u in USERS:
        db.execute(
            "INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?,?,?,?)",
            u,
        )
    try:
        db.execute(
            """
            INSERT OR IGNORE INTO agenda_zzpers (date, bus, zzper_id)
            SELECT date, bus, zzper_id FROM jobs
            WHERE zzper_id IS NOT NULL AND zzper_id != ''
            """
        )
    except sqlite3.Error:
        pass
    try:
        db.commit()
    except sqlite3.Error:
        pass


def current_user():
    if not session.get("user_id"):
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE id=?", (session["user_id"],)
    ).fetchone()


def login_required(role=None):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if role and user["role"] != role:
        flash("Geen toegang tot deze pagina.", "error")
        return redirect(url_for("login"))
    return None


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    get_db().commit()


def tl_connected():
    return bool(get_setting("tl_access_token"))


def http_json(url, payload=None, token=None, form=False):
    data = None
    headers = {"Accept": "application/json"}
    if form:
        data = urlencode(payload or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload or {}).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Teamleader fout {e.code}: {body[:400]}")


def refresh_token_if_needed():
    token = get_setting("tl_access_token")
    exp = get_setting("tl_expires_at")
    if not token:
        return None
    try:
        if exp and datetime.fromisoformat(exp) > datetime.now(timezone.utc) + timedelta(minutes=2):
            return token
    except ValueError:
        pass
    refresh = get_setting("tl_refresh_token")
    cid = get_setting("tl_client_id")
    secret = get_setting("tl_client_secret")
    if not refresh or not cid or not secret:
        return token
    data = http_json(
        "https://focus.teamleader.eu/oauth2/access_token",
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
        form=True,
    )
    save_tokens(data)
    return get_setting("tl_access_token")


def save_tokens(data):
    set_setting("tl_access_token", data.get("access_token", ""))
    if data.get("refresh_token"):
        set_setting("tl_refresh_token", data["refresh_token"])
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in") or 3600))
    set_setting("tl_expires_at", expires.isoformat())


def parse_tl_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M")
    except Exception:
        return iso[11:16] if len(iso) >= 16 else iso


def user_display_name(user):
    if not user:
        return ""
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return official_agenda_name(name, user.get("email") or "")


def pick_phone(telephones):
    if not telephones:
        return ""
    rank = {"mobile": 0, "phone": 1, "fax": 9}
    ordered = sorted(
        telephones,
        key=lambda t: rank.get(str(t.get("type") or "").lower(), 5),
    )
    for item in ordered:
        number = (item.get("number") or "").strip()
        if number:
            return number
    return ""


def parse_desc_fields(desc):
    intern = ""
    extern = ""
    phone = ""
    for line in (desc or "").splitlines():
        low = line.lower().strip()
        if low.startswith("intern:"):
            intern = line.split(":", 1)[-1].strip()
        elif low.startswith("extern:"):
            extern = line.split(":", 1)[-1].strip()
        elif low.startswith("tel:") or low.startswith("telefoon:") or low.startswith("gsm:"):
            phone = line.split(":", 1)[-1].strip()
    return intern, extern, phone


def related_party_ids(ev):
    contacts, companies = [], []
    blobs = []
    blobs.extend(ev.get("attendees") or [])
    blobs.extend(ev.get("links") or [])
    customer = ev.get("customer") or ev.get("deal") or {}
    if isinstance(customer, dict):
        blobs.append(customer)
    for item in blobs:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or item.get("type_id") or "").lower()
        ident = item.get("id")
        nested = item.get("customer") if isinstance(item.get("customer"), dict) else None
        if nested:
            typ = typ or str(nested.get("type") or "").lower()
            ident = ident or nested.get("id")
        if ident and typ == "contact":
            contacts.append(ident)
        elif ident and typ == "company":
            companies.append(ident)
    return contacts, companies


def tl_info(token, endpoint, ident, cache=None):
    if cache is None:
        cache = getattr(tl_info, "cache", None)
    if cache is None:
        cache = {}
        tl_info.cache = cache
    key = f"{endpoint}:{ident}"
    if key in cache:
        return cache[key]
    try:
        res = http_json(
            f"https://api.focus.teamleader.eu/{endpoint}",
            {"id": ident},
            token=token,
        )
        data = res.get("data") or {}
    except Exception:
        data = {}
    cache[key] = data
    return data


def contact_label(data):
    if not data:
        return ""
    if data.get("name"):
        return data.get("name")
    first = (data.get("first_name") or "").strip()
    last = (data.get("last_name") or "").strip()
    return f"{first} {last}".strip()


def enrich_from_teamleader(token, ev):
    intern, extern, phone = parse_desc_fields(ev.get("description") or "")
    contacts, companies = related_party_ids(ev)
    if not contacts and not companies and ev.get("id"):
        detail = tl_info(token, "events.info", ev.get("id"))
        if detail:
            ev.update({k: detail.get(k, ev.get(k)) for k in ("attendees", "links", "customer") if detail.get(k)})
            extra_c, extra_co = related_party_ids(detail)
            contacts.extend(extra_c)
            companies.extend(extra_co)
            if not intern or not extern or not phone:
                d_intern, d_extern, d_phone = parse_desc_fields(detail.get("description") or "")
                intern = intern or d_intern
                extern = extern or d_extern
                phone = phone or d_phone
    seen = set()
    for cid in contacts:
        if cid in seen:
            continue
        seen.add(cid)
        data = tl_info(token, "contacts.info", cid)
        extern = extern or contact_label(data)
        phone = phone or pick_phone(data.get("telephones") or data.get("phone_numbers") or [])
        if extern and phone:
            break
    if not phone or not extern:
        seen_co = set()
        for cid in companies:
            if cid in seen_co:
                continue
            seen_co.add(cid)
            data = tl_info(token, "companies.info", cid)
            extern = extern or contact_label(data)
            phone = phone or pick_phone(data.get("telephones") or data.get("phone_numbers") or [])
            if extern and phone:
                break
    return intern, extern, phone


def event_to_job(ev, bus_name, extern="", phone=""):
    start = parse_tl_time(ev.get("starts_at"))
    end = parse_tl_time(ev.get("ends_at"))
    loc = ev.get("location") or ""
    title = ev.get("title") or "Afspraak"
    desc = ev.get("description") or ""
    intern, desc_extern, desc_phone = parse_desc_fields(desc)
    extern = extern or desc_extern
    phone = phone or desc_phone
    return {
        "time": f"{start} - {end}".strip(" -"),
        "type": "Afspraak",
        "title": title,
        "details": desc,
        "bus": official_agenda_name(bus_name) if official_agenda_name(bus_name) in AGENDA_NAMES
        else official_agenda_name(intern) if official_agenda_name(intern) in AGENDA_NAMES
        else official_agenda_name(bus_name, intern) or "Onbekende agenda",
        "extern": extern,
        "phone": phone,
        "location_name": loc.split("@")[0].strip() if loc else "",
        "address": loc.split("@")[-1].strip() if "@" in loc else loc,
        "amount": "",
    }


def list_all_pages(token, endpoint, payload_filter, max_pages=20):
    items = []
    page = 1
    while page <= max_pages:
        res = http_json(
            f"https://api.focus.teamleader.eu/{endpoint}",
            {"filter": payload_filter, "page": {"size": 50, "number": page}},
            token=token,
        )
        batch = res.get("data") or []
        items.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return items


def fetch_teamleader_jobs(d):
    token = refresh_token_if_needed()
    if not token:
        return None
    tl_info.cache = {}
    start = f"{d}T00:00:00+02:00"
    end = f"{d}T23:59:59+02:00"
    users = []
    try:
        users = list_all_pages(token, "users.list", {}, max_pages=5)
    except Exception:
        users = []

    jobs = []
    if users:
        for user in users:
            uid = user.get("id")
            if not uid:
                continue
            bus_name = user_display_name(user)
            try:
                events = list_all_pages(
                    token,
                    "events.list",
                    {"user_id": uid, "ends_after": start, "starts_before": end},
                )
            except Exception:
                events = []
            for ev in events:
                intern, extern, phone = enrich_from_teamleader(token, ev)
                jobs.append(event_to_job(ev, bus_name, extern=extern, phone=phone))
    else:
        events = list_all_pages(
            token,
            "events.list",
            {"ends_after": start, "starts_before": end},
        )
        for ev in events:
            intern, extern, phone = enrich_from_teamleader(token, ev)
            jobs.append(event_to_job(ev, "Teamleader", extern=extern, phone=phone))
    return jobs


DAY_NAMES = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]


def monday_of(d):
    if isinstance(d, str):
        d = date_cls.fromisoformat(d)
    return d - timedelta(days=d.weekday())


def week_days(monday):
    return [monday + timedelta(days=i) for i in range(7)]


def decorate(jobs):
    out = []
    db = get_db()
    for j in jobs:
        item = dict(j)
        item["maps"], item["waze"] = maps_links(item.get("address") or "")
        item["inspectie"] = INSPECTIE_URL
        item["photos"] = [
            r["filename"]
            for r in db.execute(
                "SELECT filename FROM job_photos WHERE job_id=? ORDER BY id",
                (item["id"],),
            ).fetchall()
        ]
        item["photo_urls"] = [url_for("uploaded_file", filename=p) for p in item["photos"]]
        rpt = db.execute("SELECT id, status FROM dakgoot_reports WHERE job_id=?", (item["id"],)).fetchone()
        item["dakgoot_id"] = rpt["id"] if rpt else None
        item["dakgoot_status"] = rpt["status"] if rpt else None
        bvs = db.execute(
            "SELECT omschrijving, bedrag FROM bijverkoop WHERE job_id=? ORDER BY id",
            (item["id"],),
        ).fetchall()
        item["bijverkoop"] = [dict(b) for b in bvs]
        out.append(item)
    return out


@app.before_request
def setup():
    try:
        init_db()
    except Exception:
        pass


@app.errorhandler(500)
def server_error(_e):
    import traceback
    return (
        "<h1>Fout in het portaal</h1><pre style='white-space:pre-wrap'>"
        + traceback.format_exc()
        + "</pre>",
        500,
    )


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        row = get_db().execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (request.form.get("username", "").strip(), request.form.get("password", "")),
        ).fetchone()
        if not row:
            flash("Onjuiste inloggegevens.", "error")
            return render_template("login.html", title="Inloggen")
        session["user_id"] = row["id"]
        session["name"] = row["name"]
        session["role"] = row["role"]
        return redirect(url_for("admin" if row["role"] == "admin" else "zzper"))
    if current_user():
        return redirect(url_for("admin" if session.get("role") == "admin" else "zzper"))
    return render_template("login.html", title="Inloggen")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    from flask import send_from_directory
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
def admin():
    guard = login_required("admin")
    if guard:
        return guard
    d = request.args.get("date") or date_cls.today().isoformat()
    db = get_db()
    jobs = decorate(db.execute(
        "SELECT * FROM jobs WHERE date=? ORDER BY bus, time", (d,)
    ).fetchall())
    agendas = []
    current = None
    for j in jobs:
        name = j.get("bus") or "Onbekende agenda"
        if not current or current["name"] != name:
            current = {"name": name, "jobs": [], "zzpers": []}
            agendas.append(current)
        current["jobs"].append(j)
    names = [a["name"] for a in agendas]
    assigned = db.execute(
        """
        SELECT a.bus, u.id, u.name
        FROM agenda_zzpers a
        JOIN users u ON u.id = a.zzper_id
        WHERE a.date=?
        ORDER BY u.name
        """,
        (d,),
    ).fetchall()
    by_bus = {}
    for row in assigned:
        by_bus.setdefault(row["bus"], []).append(row["name"])
    for a in agendas:
        a["zzpers"] = by_bus.get(a["name"], [])
    zzpers = db.execute(
        "SELECT * FROM users WHERE role='zzper' ORDER BY name"
    ).fetchall()
    worked = db.execute(
        """
        SELECT u.id, u.name,
               COUNT(j.id) AS jobs_count,
               SUM(CASE WHEN j.payment IS NOT NULL AND j.payment != '' THEN 1 ELSE 0 END) AS done_count
        FROM users u
        JOIN agenda_zzpers a ON a.zzper_id = u.id
        JOIN jobs j ON j.date = a.date AND j.bus = a.bus
        WHERE a.date=?
        GROUP BY u.id, u.name
        ORDER BY u.name
        """,
        (d,),
    ).fetchall()
    return render_template(
        "admin.html", title="Admin", jobs=jobs, zzpers=zzpers, date=d,
        tl_connected=tl_connected(), worked=worked, agendas=agendas,
        db_path=DB_PATH,
    )


@app.route("/admin/agenda")
def admin_agenda():
    guard = login_required("admin")
    if guard:
        return guard
    d = request.args.get("date") or date_cls.today().isoformat()
    name = request.args.get("name") or ""
    db = get_db()
    jobs = decorate(db.execute(
        "SELECT * FROM jobs WHERE date=? AND bus=? ORDER BY time",
        (d, name),
    ).fetchall())
    zzpers = db.execute(
        "SELECT * FROM users WHERE role='zzper' ORDER BY name"
    ).fetchall()
    selected = {
        r["zzper_id"]
        for r in db.execute(
            "SELECT zzper_id FROM agenda_zzpers WHERE date=? AND bus=?",
            (d, name),
        ).fetchall()
    }
    return render_template(
        "admin_agenda.html", title=name, date=d, name=name,
        jobs=jobs, zzpers=zzpers, selected=selected,
    )


@app.route("/assign-agenda", methods=["POST"])
def assign_agenda():
    guard = login_required("admin")
    if guard:
        return guard
    d = request.form.get("date")
    name = request.form.get("name")
    ids = request.form.getlist("zzper_ids")
    db = get_db()
    db.execute("DELETE FROM agenda_zzpers WHERE date=? AND bus=?", (d, name))
    for zid in ids:
        db.execute(
            "INSERT OR IGNORE INTO agenda_zzpers (date, bus, zzper_id) VALUES (?,?,?)",
            (d, name, zid),
        )
        db.execute(
            "UPDATE jobs SET zzper_id=? WHERE date=? AND bus=?",
            (zid, d, name),
        )
    if not ids:
        db.execute(
            "UPDATE jobs SET zzper_id=NULL WHERE date=? AND bus=?",
            (d, name),
        )
    db.commit()
    flash(f"{name}: {len(ids)} zzper(s) gekoppeld.")
    return redirect(url_for("admin_agenda", date=d, name=name))


@app.route("/job/<int:job_id>/delete", methods=["POST"])
def delete_job(job_id):
    guard = login_required("admin")
    if guard:
        return guard
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        flash("Klus niet gevonden.", "error")
        return redirect(url_for("admin"))
    db.execute(
        "INSERT OR IGNORE INTO hidden_jobs (date, time, title, bus) VALUES (?,?,?,?)",
        (job["date"], job["time"], job["title"], job["bus"]),
    )
    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    db.commit()
    flash("Klus verwijderd uit het portaal.")
    return redirect(url_for("admin_agenda", date=job["date"], name=job["bus"]))


@app.route("/admin/zzper/<int:zzper_id>")
def admin_zzper_day(zzper_id):
    guard = login_required("admin")
    if guard:
        return guard
    d = request.args.get("date") or date_cls.today().isoformat()
    db = get_db()
    person = db.execute("SELECT * FROM users WHERE id=? AND role='zzper'", (zzper_id,)).fetchone()
    if not person:
        flash("Zzper niet gevonden.", "error")
        return redirect(url_for("admin", date=d))
    jobs = decorate(db.execute(
        """
        SELECT j.* FROM jobs j
        JOIN agenda_zzpers a ON a.date = j.date AND a.bus = j.bus
        WHERE j.date=? AND a.zzper_id=?
        ORDER BY j.time
        """,
        (d, zzper_id),
    ).fetchall())
    return render_template(
        "admin_zzper.html", title=person["name"], person=person, jobs=jobs, date=d
    )


@app.route("/sync", methods=["POST"])
def sync():
    guard = login_required("admin")
    if guard:
        return guard
    d = request.form.get("date") or date_cls.today().isoformat()
    db = get_db()
    source = "voorbeeldagenda"
    incoming = SAMPLE_JOBS
    if tl_connected():
        try:
            fetched = fetch_teamleader_jobs(d)
            if fetched is not None:
                incoming = fetched
                source = "Teamleader"
        except Exception as e:
            flash(f"Teamleader ophalen mislukt: {e}", "error")
            return redirect(url_for("admin", date=d))

    replace = request.form.get("replace") == "1"
    existing = db.execute("SELECT COUNT(*) AS c FROM jobs WHERE date=?", (d,)).fetchone()["c"]
    added = 0
    if existing and replace:
        db.execute("DELETE FROM jobs WHERE date=?", (d,))
        existing = 0
    if existing == 0:
        hidden = {
            (r["time"], r["title"], r["bus"])
            for r in db.execute(
                "SELECT time, title, bus FROM hidden_jobs WHERE date=?", (d,)
            ).fetchall()
        }
        for j in incoming:
            if (j["time"], j["title"], j["bus"]) in hidden:
                continue
            db.execute(
                """
                INSERT INTO jobs
                (date, time, type, title, details, bus, extern, phone, location_name, address, amount)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    d, j["time"], j["type"], j["title"], j["details"], j["bus"],
                    j["extern"], j["phone"], j["location_name"], j["address"], j["amount"],
                ),
            )
            added += 1
        db.commit()
        flash(f"Gesynchroniseerd: {added} jobs voor {d} ({source}).")
    else:
        flash("Deze datum stond al in het portaal. Geen dubbele jobs toegevoegd.")
    return redirect(url_for("admin", date=d))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    guard = login_required("admin")
    if guard:
        return guard
    if request.method == "POST":
        set_setting("tl_client_id", request.form.get("tl_client_id", "").strip())
        set_setting("tl_client_secret", request.form.get("tl_client_secret", "").strip())
        set_setting("smtp_host", request.form.get("smtp_host", "").strip())
        set_setting("smtp_port", request.form.get("smtp_port", "").strip() or "587")
        set_setting("smtp_user", request.form.get("smtp_user", "").strip())
        if request.form.get("smtp_pass", "").strip():
            set_setting("smtp_pass", request.form.get("smtp_pass", "").strip())
        set_setting("smtp_from", request.form.get("smtp_from", "").strip())
        flash("Instellingen opgeslagen.")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        title="Teamleader",
        tl_client_id=get_setting("tl_client_id"),
        tl_client_secret=get_setting("tl_client_secret"),
        tl_connected=tl_connected(),
        redirect_uri=url_for("teamleader_callback", _external=True),
        smtp_host=get_setting("smtp_host"),
        smtp_port=get_setting("smtp_port") or "587",
        smtp_user=get_setting("smtp_user"),
        smtp_pass=get_setting("smtp_pass"),
        smtp_from=get_setting("smtp_from"),
    )


@app.route("/teamleader/connect")
def teamleader_connect():
    guard = login_required("admin")
    if guard:
        return guard
    cid = get_setting("tl_client_id")
    if not cid:
        flash("Vul eerst Client ID en Secret in.", "error")
        return redirect(url_for("settings"))
    state = secrets.token_urlsafe(16)
    session["tl_state"] = state
    qs = urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": url_for("teamleader_callback", _external=True),
        "state": state,
    })
    return redirect(f"https://focus.teamleader.eu/oauth2/authorize?{qs}")


@app.route("/teamleader/callback")
def teamleader_callback():
    guard = login_required("admin")
    if guard:
        return guard
    if request.args.get("state") != session.get("tl_state"):
        flash("Beveiligingscontrole mislukt. Probeer opnieuw te verbinden.", "error")
        return redirect(url_for("settings"))
    code = request.args.get("code")
    if not code:
        flash("Teamleader gaf geen code terug.", "error")
        return redirect(url_for("settings"))
    try:
        data = http_json(
            "https://focus.teamleader.eu/oauth2/access_token",
            {
                "client_id": get_setting("tl_client_id"),
                "client_secret": get_setting("tl_client_secret"),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": url_for("teamleader_callback", _external=True),
            },
            form=True,
        )
        save_tokens(data)
        flash("Teamleader is gekoppeld. Je kunt nu synchroniseren.")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("settings"))


@app.route("/assign", methods=["POST"])
def assign():
    guard = login_required("admin")
    if guard:
        return guard
    d = request.form.get("date")
    db = get_db()
    buses = [r["bus"] for r in db.execute(
        "SELECT DISTINCT bus FROM jobs WHERE date=? ORDER BY bus", (d,)
    ).fetchall()]
    for i, bus in enumerate(buses):
        val = request.form.get(f"agenda_{i}") or None
        db.execute(
            "UPDATE jobs SET zzper_id=? WHERE date=? AND bus=?",
            (val, d, bus),
        )
    db.commit()
    flash("Agenda's toegewezen aan zzpers.")
    return redirect(url_for("admin", date=d))


@app.route("/zzper")
def zzper():
    guard = login_required("zzper")
    if guard:
        return guard
    d = request.args.get("date") or date_cls.today().isoformat()
    jobs = decorate(get_db().execute(
        """
        SELECT j.* FROM jobs j
        JOIN agenda_zzpers a ON a.date = j.date AND a.bus = j.bus
        WHERE j.date=? AND a.zzper_id=?
        ORDER BY j.time
        """,
        (d, session["user_id"]),
    ).fetchall())
    return render_template("zzper.html", title="Mijn dag", jobs=jobs, date=d)


@app.route("/report/<int:job_id>", methods=["POST"])
def report(job_id):
    guard = login_required("zzper")
    if guard:
        return guard
    d = request.form.get("date")
    payment = request.form.get("payment")
    comment = request.form.get("comment", "").strip()
    if payment not in ("contant", "gepind", "factuur"):
        flash("Kies een geldige betaalwijze.", "error")
        return redirect(url_for("zzper", date=d))
    db = get_db()
    job = db.execute(
        """
        SELECT j.* FROM jobs j
        JOIN agenda_zzpers a ON a.date = j.date AND a.bus = j.bus
        WHERE j.id=? AND a.zzper_id=?
        """,
        (job_id, session["user_id"]),
    ).fetchone()
    if not job:
        flash("Job niet gevonden of niet van jou.", "error")
        return redirect(url_for("zzper", date=d))
    db.execute(
        "UPDATE jobs SET payment=?, comment=? WHERE id=?",
        (payment, comment, job_id),
    )
    db.commit()
    flash("Terugmelding opgeslagen.")
    return redirect(url_for("zzper", date=d))


@app.route("/job/<int:job_id>/bijverkoop", methods=["POST"])
def add_bijverkoop(job_id):
    user = current_user()
    if not user or user["role"] != "zzper":
        return redirect(url_for("login"))
    if not job_visible_for_zzper(job_id, user["id"]):
        flash("Deze klus is niet van jou.", "error")
        return redirect(url_for("zzper"))
    oms = request.form.get("omschrijving", "").strip()
    try:
        bedrag = float(request.form.get("bedrag", "0").replace(",", "."))
    except ValueError:
        bedrag = 0
    if not oms or bedrag <= 0:
        flash("Vul omschrijving en bedrag in.", "error")
        return redirect(url_for("zzper", date=request.form.get("date")))
    db = get_db()
    job = db.execute("SELECT date FROM jobs WHERE id=?", (job_id,)).fetchone()
    db.execute(
        "INSERT INTO bijverkoop (job_id, zzper_id, omschrijving, bedrag, created_at, date) VALUES (?,?,?,?,?,?)",
        (job_id, user["id"], oms, bedrag, datetime.now(timezone.utc).isoformat(), job["date"] if job else ""),
    )
    db.commit()
    flash("Bijverkoop toegevoegd.")
    return redirect(url_for("zzper", date=request.form.get("date") or date_cls.today().isoformat()))


@app.route("/admin/bijverkoop")
def admin_bijverkoop():
    guard = login_required("admin")
    if guard:
        return guard
    period = request.args.get("period") or "week"
    start = request.args.get("start") or date_cls.today().isoformat()
    db = get_db()
    # calculate range
    d = date_cls.fromisoformat(start)
    if period == "week":
        start_d = d - timedelta(days=d.weekday())
        end_d = start_d + timedelta(days=6)
    elif period == "maand":
        start_d = d.replace(day=1)
        if d.month == 12:
            end_d = d.replace(year=d.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end_d = d.replace(month=d.month + 1, day=1) - timedelta(days=1)
    elif period == "kwartaal":
        q = (d.month - 1) // 3
        start_d = date_cls(d.year, q * 3 + 1, 1)
        if q == 3:
            end_d = date_cls(d.year + 1, 1, 1) - timedelta(days=1)
        else:
            end_d = date_cls(d.year, (q + 1) * 3 + 1, 1) - timedelta(days=1)
    else:  # jaar
        start_d = date_cls(d.year, 1, 1)
        end_d = date_cls(d.year, 12, 31)
    rows = db.execute(
        """
        SELECT b.*, u.name as zzper_name, j.title, j.address
        FROM bijverkoop b
        JOIN users u ON u.id = b.zzper_id
        LEFT JOIN jobs j ON j.id = b.job_id
        WHERE b.date BETWEEN ? AND ?
        ORDER BY b.date DESC, u.name
        """,
        (start_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    totals = db.execute(
        """
        SELECT u.name, SUM(b.bedrag) as totaal, SUM(b.bonus) as bonus_totaal, COUNT(*) as aantal
        FROM bijverkoop b
        JOIN users u ON u.id = b.zzper_id
        WHERE b.date BETWEEN ? AND ?
        GROUP BY u.id, u.name
        ORDER BY totaal DESC
        """,
        (start_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    return render_template(
        "admin_bijverkoop.html",
        title="Bijverkoop",
        rows=rows,
        totals=totals,
        period=period,
        start=start_d.isoformat(),
        end=end_d.isoformat(),
    )


@app.route("/admin/bijverkoop/<int:bv_id>/bonus", methods=["POST"])
def set_bijverkoop_bonus(bv_id):
    guard = login_required("admin")
    if guard:
        return guard
    try:
        bonus = float(request.form.get("bonus", "0").replace(",", "."))
    except ValueError:
        bonus = 0
    get_db().execute("UPDATE bijverkoop SET bonus=? WHERE id=?", (bonus, bv_id))
    get_db().commit()
    flash("Bonus opgeslagen.")
    return redirect(request.referrer or url_for("admin_bijverkoop"))


@app.route("/admin/bijverkoop/export")
def export_bijverkoop():
    guard = login_required("admin")
    if guard:
        return guard
    period = request.args.get("period") or "week"
    start = request.args.get("start") or date_cls.today().isoformat()
    d = date_cls.fromisoformat(start)
    if period == "week":
        start_d = d - timedelta(days=d.weekday())
        end_d = start_d + timedelta(days=6)
    elif period == "maand":
        start_d = d.replace(day=1)
        end_d = (start_d.replace(month=start_d.month % 12 + 1, day=1) if start_d.month < 12 else date_cls(start_d.year + 1, 1, 1)) - timedelta(days=1)
    elif period == "kwartaal":
        q = (d.month - 1) // 3
        start_d = date_cls(d.year, q * 3 + 1, 1)
        end_d = (date_cls(d.year, (q + 1) * 3 + 1, 1) if q < 3 else date_cls(d.year + 1, 1, 1)) - timedelta(days=1)
    else:
        start_d = date_cls(d.year, 1, 1)
        end_d = date_cls(d.year, 12, 31)
    rows = get_db().execute(
        """
        SELECT b.date, u.name, b.omschrijving, b.bedrag, b.bonus, j.title, j.address
        FROM bijverkoop b
        JOIN users u ON u.id = b.zzper_id
        LEFT JOIN jobs j ON j.id = b.job_id
        WHERE b.date BETWEEN ? AND ?
        ORDER BY b.date, u.name
        """,
        (start_d.isoformat(), end_d.isoformat()),
    ).fetchall()
    import csv, io
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Datum", "Zzper", "Omschrijving", "Bedrag", "Bonus", "Klus", "Adres"])
    for r in rows:
        w.writerow([r["date"], r["name"], r["omschrijving"], r["bedrag"], r["bonus"], r["title"] or "", r["address"] or ""])
    from flask import Response
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=bijverkoop-{period}-{start_d}.csv"},
    )


def job_visible_for_zzper(job_id, zzper_id):
    return get_db().execute(
        """
        SELECT j.id FROM jobs j
        JOIN agenda_zzpers a ON a.date = j.date AND a.bus = j.bus
        WHERE j.id=? AND a.zzper_id=?
        """,
        (job_id, zzper_id),
    ).fetchone()


@app.route("/job/<int:job_id>/photo", methods=["POST"])
def upload_photo(job_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    d = request.form.get("date") or date_cls.today().isoformat()
    name = request.form.get("name") or ""
    if user["role"] == "zzper" and not job_visible_for_zzper(job_id, user["id"]):
        flash("Deze klus is niet van jou.", "error")
        return redirect(url_for("zzper", date=d))
    if user["role"] not in ("admin", "zzper"):
        return redirect(url_for("login"))
    f = request.files.get("photo")
    if not f or not f.filename:
        flash("Kies een foto.", "error")
        return redirect(request.referrer or url_for("zzper", date=d))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_PHOTOS:
        flash("Alleen jpg, png, webp of heic.", "error")
        return redirect(request.referrer or url_for("zzper", date=d))
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fname = f"{job_id}_{uuid.uuid4().hex[:10]}{ext}"
    safe = secure_filename(fname)
    f.save(os.path.join(UPLOAD_DIR, safe))
    get_db().execute(
        "INSERT INTO job_photos (job_id, filename) VALUES (?,?)",
        (job_id, safe),
    )
    get_db().commit()
    flash("Foto toegevoegd.")
    if user["role"] == "admin":
        return redirect(url_for("admin_agenda", date=d, name=name))
    return redirect(url_for("zzper", date=d))


@app.route("/team", methods=["GET", "POST"])
def team():
    guard = login_required("admin")
    if guard:
        return guard
    db = get_db()
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "").strip()
        if not first or not last:
            flash("Vul voornaam en achternaam in.", "error")
            return redirect(url_for("team"))
        if not username:
            username = (first + last).lower().replace(" ", "")
        if not password:
            password = username + "123"
        name = f"{first} {last}".strip()
        try:
            db.execute(
                "INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
                (username, password, "zzper", name),
            )
            db.commit()
            flash(f"{name} toegevoegd. Inloggen: {username} / {password}")
        except sqlite3.IntegrityError:
            flash("Deze inlognaam bestaat al. Kies een andere.", "error")
        return redirect(url_for("team"))
    zzpers = db.execute(
        "SELECT * FROM users WHERE role='zzper' ORDER BY name"
    ).fetchall()
    return render_template("team.html", title="Zzpers", zzpers=zzpers)


@app.route("/team/<int:user_id>/delete", methods=["POST"])
def team_delete(user_id):
    guard = login_required("admin")
    if guard:
        return guard
    db = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE id=? AND role='zzper'", (user_id,)
    ).fetchone()
    if not row:
        flash("Zzper niet gevonden.", "error")
        return redirect(url_for("team"))
    db.execute("DELETE FROM agenda_zzpers WHERE zzper_id=?", (user_id,))
    db.execute("DELETE FROM week_roster WHERE zzper_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash(f"{row['name']} verwijderd.")
    return redirect(url_for("team"))


@app.route("/rooster", methods=["GET", "POST"])
def rooster():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    start = request.values.get("week") or monday_of(date_cls.today()).isoformat()
    monday = monday_of(start)
    days = week_days(monday)
    db = get_db()
    zzpers = db.execute("SELECT * FROM users WHERE role='zzper' ORDER BY name").fetchall()

    if request.method == "POST":
        if user["role"] != "admin":
            flash("Geen toegang.", "error")
            return redirect(url_for("rooster", week=monday.isoformat()))
        for day in days:
            db.execute("DELETE FROM week_roster WHERE date=?", (day.isoformat(),))
            for zid in request.form.getlist(f"day_{day.isoformat()}"):
                db.execute(
                    "INSERT OR IGNORE INTO week_roster (date, zzper_id) VALUES (?,?)",
                    (day.isoformat(), zid),
                )
        db.commit()
        flash("Weekrooster opgeslagen.")
        return redirect(url_for("rooster", week=monday.isoformat()))

    rows = db.execute(
        """
        SELECT r.date, r.zzper_id, u.name
        FROM week_roster r
        JOIN users u ON u.id = r.zzper_id
        WHERE r.date BETWEEN ? AND ?
        ORDER BY u.name
        """,
        (days[0].isoformat(), days[6].isoformat()),
    ).fetchall()
    by_day = {day.isoformat(): [] for day in days}
    my_days = set()
    for r in rows:
        by_day[r["date"]].append(r)
        if r["zzper_id"] == session.get("user_id"):
            my_days.add(r["date"])

    prev_week = (monday - timedelta(days=7)).isoformat()
    next_week = (monday + timedelta(days=7)).isoformat()
    return render_template(
        "rooster.html",
        title="Weekrooster",
        days=list(zip(DAY_NAMES, days)),
        by_day=by_day,
        my_days=my_days,
        zzpers=zzpers,
        monday=monday.isoformat(),
        sunday=days[6].isoformat(),
        prev_week=prev_week,
        next_week=next_week,
        is_admin=user["role"] == "admin",
        tab="rooster",
    )


DAKGOOT_PUNTEN = [
    "Dakgoten schoon van blad en vervuiling",
    "Hemelwaterafvoer gecontroleerd op verstoppingen",
    "Geen zichtbare beschadigingen of lekkages",
    "Goede afwatering",
    "Bevestiging en ophanging in orde",
]


@app.route("/job/<int:job_id>/dakgoot", methods=["GET", "POST"])
def dakgoot_report(job_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        flash("Klus niet gevonden.", "error")
        return redirect(url_for("zzper"))
    if user["role"] == "zzper" and not job_visible_for_zzper(job_id, user["id"]):
        flash("Deze klus is niet van jou.", "error")
        return redirect(url_for("zzper"))
    report = db.execute("SELECT * FROM dakgoot_reports WHERE job_id=?", (job_id,)).fetchone()
    photos = []
    if report:
        photos = [r["filename"] for r in db.execute(
            "SELECT filename FROM report_photos WHERE report_id=? ORDER BY id", (report["id"],)
        ).fetchall()]

    if request.method == "POST":
        status = request.form.get("status")
        comment = request.form.get("comment", "").strip()
        if status not in ("goedgekeurd", "afgekeurd"):
            flash("Kies goedgekeurd of afgekeurd.", "error")
            return redirect(url_for("dakgoot_report", job_id=job_id))
        if status == "afgekeurd" and not comment:
            flash("Bij afkeuring is een opmerking verplicht.", "error")
            return redirect(url_for("dakgoot_report", job_id=job_id))
        address = job["address"] or ""
        now = datetime.now(timezone.utc).isoformat()
        if report:
            db.execute(
                "UPDATE dakgoot_reports SET status=?, comment=?, address=?, created_at=? WHERE id=?",
                (status, comment if status == "afgekeurd" else "", address, now, report["id"]),
            )
            report_id = report["id"]
        else:
            cur = db.execute(
                "INSERT INTO dakgoot_reports (job_id, status, comment, address, created_at, zzper_id) VALUES (?,?,?,?,?,?)",
                (job_id, status, comment if status == "afgekeurd" else "", address, now, user["id"]),
            )
            report_id = cur.lastrowid
        # photos
        files = request.files.getlist("photos")
        uploaded = 0
        for f in files:
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_PHOTOS:
                continue
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            fname = f"rpt_{report_id}_{uuid.uuid4().hex[:10]}{ext}"
            safe = secure_filename(fname)
            f.save(os.path.join(UPLOAD_DIR, safe))
            db.execute("INSERT INTO report_photos (report_id, filename) VALUES (?,?)", (report_id, safe))
            uploaded += 1
        if uploaded == 0 and not photos:
            flash("Voeg minimaal 1 foto toe.", "error")
            db.rollback()
            return redirect(url_for("dakgoot_report", job_id=job_id))
        db.commit()
        flash("Dakgoot-rapport opgeslagen.")
        if user["role"] == "admin":
            return redirect(url_for("view_dakgoot", report_id=report_id))
        return redirect(url_for("zzper", date=job["date"]))

    return render_template(
        "dakgoot.html",
        title="Dakgoot rapport",
        job=job,
        report=report,
        photos=photos,
        punten=DAKGOOT_PUNTEN,
        is_admin=user["role"] == "admin",
    )


@app.route("/dakgoot/<int:report_id>")
def view_dakgoot(report_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    report = db.execute("SELECT * FROM dakgoot_reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        flash("Rapport niet gevonden.", "error")
        return redirect(url_for("admin"))
    job = db.execute("SELECT * FROM jobs WHERE id=?", (report["job_id"],)).fetchone()
    photos = [r["filename"] for r in db.execute(
        "SELECT filename FROM report_photos WHERE report_id=? ORDER BY id", (report_id,)
    ).fetchall()]
    zzper = db.execute("SELECT name FROM users WHERE id=?", (report["zzper_id"],)).fetchone()
    return render_template(
        "dakgoot_view.html",
        title="Dakgoot rapport",
        report=report,
        job=job,
        photos=photos,
        punten=DAKGOOT_PUNTEN,
        zzper_name=zzper["name"] if zzper else "",
        is_admin=user["role"] == "admin",
    )


def build_dakgoot_pdf(report, job, photos, zzper_name):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    # Navy header bar
    pdf.set_fill_color(27, 33, 86)
    pdf.rect(0, 0, 210, 32, "F")
    # Coral accent line
    pdf.set_fill_color(242, 122, 122)
    pdf.rect(0, 32, 210, 3, "F")
    # Logo text
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(12, 10)
    pdf.cell(0, 8, "Huisconcierge", ln=True)
    pdf.set_font("Helvetica", size=9)
    pdf.set_xy(12, 19)
    pdf.cell(0, 6, "Dakgoot-rapport", ln=True)
    pdf.set_text_color(27, 33, 86)
    pdf.set_y(42)
    pdf.set_font("Helvetica", size=11)
    address = report["address"] or (job["address"] if job else "") or "-"
    pdf.cell(0, 7, f"Adres: {address}", ln=True)
    if job and job["location_name"]:
        pdf.cell(0, 7, f"Locatie: {job['location_name']}", ln=True)
    pdf.cell(0, 7, f"Datum: {job['date'] if job else ''}", ln=True)
    pdf.cell(0, 7, f"Ingevuld door: {zzper_name or '-'}", ln=True)
    pdf.ln(4)
    status_txt = "Goedgekeurd" if report["status"] == "goedgekeurd" else "Afgekeurd"
    pdf.set_font("Helvetica", "B", 13)
    if report["status"] == "goedgekeurd":
        pdf.set_text_color(22, 101, 52)
    else:
        pdf.set_text_color(153, 27, 27)
    pdf.cell(0, 8, f"Status: {status_txt}", ln=True)
    pdf.set_text_color(27, 33, 86)
    pdf.set_font("Helvetica", size=11)
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Controlepunten", ln=True)
    pdf.set_font("Helvetica", size=11)
    fixed = [
        "Dakgoten schoon van blad en vervuiling",
        "Hemelwaterafvoer gecontroleerd op verstoppingen",
    ]
    for p in fixed:
        pdf.cell(0, 7, f"  [v]  {p}", ln=True)
    if report["status"] == "goedgekeurd":
        for p in DAKGOOT_PUNTEN[2:]:
            pdf.cell(0, 7, f"  [v]  {p}", ln=True)
    else:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, "Opmerking bij afkeuring:", ln=True)
        pdf.set_font("Helvetica", size=11)
        pdf.multi_cell(0, 6, report["comment"] or "-")
    if photos:
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, "Foto's", ln=True)
        y = pdf.get_y() + 2
        for i, fn in enumerate(photos[:2]):
            path = os.path.join(UPLOAD_DIR, fn)
            if os.path.exists(path):
                try:
                    if os.path.getsize(path) > 3_000_000:
                        continue
                    pdf.image(path, x=10 + (i % 2) * 95, y=y, w=88)
                except Exception:
                    pass
        pdf.ln(58)
    pdf.set_y(-18)
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(91, 98, 128)
    pdf.cell(0, 5, "Huisconcierge  ·  huisconcierge.nl  ·  Dakgoot-rapport", align="C")
    pdf_path = os.path.join("/tmp", f"dakgoot_{report['id']}.pdf")
    pdf.output(pdf_path)
    return pdf_path


@app.route("/dakgoot/<int:report_id>/pdf")
def dakgoot_pdf(report_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    db = get_db()
    report = db.execute("SELECT * FROM dakgoot_reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        flash("Rapport niet gevonden.", "error")
        return redirect(url_for("admin"))
    job = db.execute("SELECT * FROM jobs WHERE id=?", (report["job_id"],)).fetchone()
    photos = [r["filename"] for r in db.execute(
        "SELECT filename FROM report_photos WHERE report_id=? ORDER BY id", (report_id,)
    ).fetchall()]
    zzper = db.execute("SELECT name FROM users WHERE id=?", (report["zzper_id"],)).fetchone()
    path = build_dakgoot_pdf(report, job, photos, zzper["name"] if zzper else "")
    from flask import send_file
    return send_file(path, as_attachment=True, download_name=f"dakgoot-rapport-{report_id}.pdf")


@app.route("/dakgoot/<int:report_id>/email", methods=["POST"])
def email_dakgoot(report_id):
    guard = login_required("admin")
    if guard:
        return guard
    to_email = request.form.get("email", "").strip()
    if not to_email or "@" not in to_email:
        flash("Vul een geldig e-mailadres in.", "error")
        return redirect(url_for("view_dakgoot", report_id=report_id))
    db = get_db()
    report = db.execute("SELECT * FROM dakgoot_reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        flash("Rapport niet gevonden.", "error")
        return redirect(url_for("admin"))
    job = db.execute("SELECT * FROM jobs WHERE id=?", (report["job_id"],)).fetchone()
    photos = [r["filename"] for r in db.execute(
        "SELECT filename FROM report_photos WHERE report_id=?", (report_id,)
    ).fetchall()]
    zzper = db.execute("SELECT name FROM users WHERE id=?", (report["zzper_id"],)).fetchone()
    status_txt = "Goedgekeurd" if report["status"] == "goedgekeurd" else "Afgekeurd"
    body = (
        f"Beste klant,\n\n"
        f"Hierbij het dakgoot-rapport van Huisconcierge.\n\n"
        f"Adres: {report['address'] or (job['address'] if job else '')}\n"
        f"Status: {status_txt}\n"
    )
    if report["status"] == "afgekeurd":
        body += f"Opmerking: {report['comment'] or ''}\n"
    body += "\nMet vriendelijke groet,\nHuisconcierge"
    pdf_path = build_dakgoot_pdf(report, job, photos, zzper["name"] if zzper else "")
    smtp_host = get_setting("smtp_host")
    smtp_user = get_setting("smtp_user")
    smtp_pass = get_setting("smtp_pass")
    from_addr = get_setting("smtp_from") or smtp_user
    if smtp_host and smtp_user and smtp_pass and from_addr:
        try:
            import smtplib
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = f"Dakgoot-rapport – {report['address'] or 'Huisconcierge'}"
            msg["From"] = from_addr
            msg["To"] = to_email
            msg.set_content(body)
            with open(pdf_path, "rb") as f:
                msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"dakgoot-rapport-{report_id}.pdf")
            with smtplib.SMTP(smtp_host, int(get_setting("smtp_port") or 587), timeout=20) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)
            flash(f"Rapport (PDF) verstuurd naar {to_email}.")
        except Exception as e:
            flash(f"E-mail mislukt: {e}. Download de PDF en stuur hem zelf.", "error")
    else:
        flash("SMTP nog niet ingesteld. Download de PDF en stuur hem zelf per e-mail.")
    return redirect(url_for("view_dakgoot", report_id=report_id))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
