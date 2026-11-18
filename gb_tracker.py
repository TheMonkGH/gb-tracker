#!/usr/bin/env python3
"""
GameBanana Tracker - suit likes / téléchargements / vues de tes mods
et les compare aux autres mods de la même catégorie.

Aucune dépendance : Python 3.8+ standard seulement.

Commandes :
  python gb_tracker.py            -> relevé du jour + régénère dashboard.html
  python gb_tracker.py secours    -> relevé de secours : ne fait rien si le relevé
                                     du jour a déjà réussi, sinon fait le relevé
  python gb_tracker.py dashboard  -> régénère seulement dashboard.html
  python gb_tracker.py probe URL  -> affiche le JSON brut d'une URL de l'API (debug)
  python gb_tracker.py demo       -> crée demo.db avec 60 jours de fausses données
                                     et demo_dashboard.html pour voir le rendu
  python gb_tracker.py exporter-csv [base.db]
                                  -> convertit tes relevés SQLite en fichiers CSV
                                     (data/) pour passer à la version GitHub
"""
import csv
import glob
import io
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

# Noms de mods exotiques (japonais, emoji...) : ne jamais planter sur l'affichage console.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
API = "https://gamebanana.com/apiv11"
UA = "gb-tracker/1.0 (suivi perso de stats de mods)"

DEFAULT_CONFIG = {
    "member_id": 4254243,
    "mod_ids": [],                 # à remplir si la découverte auto échoue
    "max_mods_per_category": 0,    # 0 = toute la catégorie ; sinon plafond (les plus téléchargés d'abord)
    "delay_seconds": 0.5,          # pause mini entre deux requêtes ; augmente toute seule si GB freine
    "storage": "sqlite",           # "sqlite" (PC) ou "csv" (dossier data/, pour GitHub)
    "db_file": "gb_stats.db",
    "data_dir": "data",
    "dashboard_file": "dashboard.html",
}

# --------------------------------------------------------------------------
# Config / logs
# --------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(json.load(f))
    return cfg


def write_atomic(path, text):
    """Écrit dans un fichier temporaire puis le substitue d'un coup :
    jamais de fichier à moitié écrit si le PC s'éteint pendant l'écriture."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_config(cfg):
    write_atomic(CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False))


LOG_PATH = os.path.join(HERE, "gb_tracker.log")
LOG_MAX = 1_000_000  # au-delà de ~1 Mo, l'ancien log devient gb_tracker.log.old


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # un log impossible à écrire ne doit pas faire échouer le relevé

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Blocked(Exception):
    """GameBanana refuse nos requêtes de façon répétée : on arrête le relevé."""


class Offline(Exception):
    """Pas de connexion Internet (ou GameBanana injoignable) : on n'essaie même pas."""


NETWORK_WAIT = 600   # au démarrage du PC, attendre jusqu'à 10 min que le réseau soit prêt
NETWORK_RETRY = 30


def wait_for_network(member_id):
    """Après un rallumage, la tâche peut partir avant que le Wi-Fi soit connecté.
    On vérifie d'abord que GameBanana répond (n'importe quelle réponse HTTP, même
    une erreur, prouve que le réseau marche), en réessayant toutes les 30 s."""
    url = f"{API}/Member/{int(member_id or 1)}/ProfilePage"
    deadline = time.time() + NETWORK_WAIT
    first = True
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError:
            return  # le serveur a répondu : le réseau marche
        except Exception as e:
            if time.time() + NETWORK_RETRY > deadline:
                raise Offline(f"GameBanana injoignable depuis {NETWORK_WAIT // 60} min ({e})")
            if first:
                log(f"Pas encore de réseau ({e}) : nouvel essai toutes les {NETWORK_RETRY} s, "
                    f"pendant {NETWORK_WAIT // 60} min maximum.")
                first = False
            time.sleep(NETWORK_RETRY)


class Api:
    """Client HTTP avec pause adaptative.

    - démarre avec delay_seconds entre deux requêtes ;
    - au moindre signe de freinage (HTTP 429, 503, ou page HTML/anti-bot à la
      place du JSON), multiplie la pause par 1,5, respecte Retry-After et réessaie ;
    - après 15 requêtes réussies d'affilée, raccourcit la pause de 25 %
      (sans descendre sous delay_seconds) ;
    - après 6 requêtes d'affilée bloquées malgré les réessais, lève Blocked :
      le relevé s'arrête proprement au lieu d'insister.
    """
    MAX_DELAY = 30.0

    def __init__(self, delay):
        self.min_delay = max(0.0, float(delay))
        self.delay = self.min_delay
        self._last = 0.0
        self._streak_ok = 0
        self._streak_blocked = 0
        self.stats = {"requests": 0, "throttled": 0, "errors": 0, "max_delay": self.min_delay}

    def _throttled(self, retry_after=None):
        self.stats["throttled"] += 1
        self._streak_ok = 0
        self.delay = min(self.MAX_DELAY, max(self.delay * 1.5, 1.0))
        self.stats["max_delay"] = max(self.stats["max_delay"], self.delay)
        time.sleep(retry_after if retry_after else self.delay * 3)

    def get(self, path_or_url, retries=4):
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        for attempt in range(retries):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.stats["requests"] += 1
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", "replace")
                try:
                    data = json.loads(body)
                except ValueError:
                    # 200 mais pas du JSON : typiquement une page anti-bot
                    self._throttled()
                    continue
                self._streak_blocked = 0
                self._streak_ok += 1
                if self._streak_ok >= 15 and self.delay > self.min_delay:
                    self.delay = max(self.min_delay, self.delay * 0.75)
                    self._streak_ok = 0
                return data
            except urllib.error.HTTPError as e:
                ctype = e.headers.get("Content-Type", "") if e.headers else ""
                is_block = e.code in (429, 503) or (e.code == 403 and "html" in ctype)
                if is_block:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    ra = float(ra) if ra and ra.isdigit() else None
                    if ra and ra > 120:
                        # GB demande d'attendre longtemps : inutile d'insister aujourd'hui
                        raise Blocked(f"GameBanana demande d'attendre {ra:.0f} s")
                    self._throttled(ra)
                    continue
                if e.code in (500, 502, 504) and attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                self.stats["errors"] += 1
                return {"__error__": f"HTTP {e.code}"}   # 400/404... : réponse normale, pas un blocage
            except Exception as e:  # réseau coupé, timeout...
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                self.stats["errors"] += 1
                return {"__error__": str(e)}
        # tous les essais ont été freinés
        self._streak_blocked += 1
        if self._streak_blocked >= 6:
            raise Blocked(f"GameBanana bloque les requêtes (pause montée à {self.delay:.0f} s)")
        self.stats["errors"] += 1
        return {"__error__": "freiné"}

# --------------------------------------------------------------------------
# Lecture des réponses
# --------------------------------------------------------------------------

def records_of(payload):
    """Renvoie la liste de mods d'une réponse, quel que soit son format."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("_aRecords", "records", "_aSubmissions"):
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    return []


def cat_ids_of(rec):
    """Toutes les catégories (id) auxquelles un enregistrement appartient."""
    ids = set()
    for k in ("_aCategory", "_aSuperCategory", "_aRootCategory", "_aSubCategory"):
        c = rec.get(k)
        if isinstance(c, dict):
            if c.get("_idRow"):
                ids.add(int(c["_idRow"]))
            m = re.search(r"/cats/(\d+)", c.get("_sProfileUrl", "") or "")
            if m:
                ids.add(int(m.group(1)))
    return ids


def is_mod(rec):
    return rec.get("_sModelName", "Mod") == "Mod" and rec.get("_idRow")


def profile(api, mod_id):
    p = api.get(f"/Mod/{int(mod_id)}/ProfilePage")
    if "__error__" in p or not p.get("_idRow"):
        return None
    if p.get("_bIsTrashed") or p.get("_bIsPrivate") or p.get("_bIsWithheld"):
        return None
    cat = p.get("_aCategory") or {}
    sup = p.get("_aSuperCategory") or {}
    game = p.get("_aGame") or {}
    return {
        "mod_id": int(p["_idRow"]),
        "name": p.get("_sName", "?"),
        "url": p.get("_sProfileUrl", f"https://gamebanana.com/mods/{mod_id}"),
        "submitter": (p.get("_aSubmitter") or {}).get("_sName", ""),
        "submitter_id": int((p.get("_aSubmitter") or {}).get("_idRow") or 0),
        "game": game.get("_sName", ""),
        "cat_id": int(cat.get("_idRow") or 0),
        "cat_name": cat.get("_sName", ""),
        "supercat_id": int(sup.get("_idRow") or 0),
        "supercat_name": sup.get("_sName", ""),
        "date_added": int(p.get("_tsDateAdded") or 0),
        "downloads": int(p.get("_nDownloadCount") or 0),
        "likes": int(p.get("_nLikeCount") or 0),
        "views": int(p.get("_nViewCount") or 0),
        "posts": int(p.get("_nPostCount") or 0),
        "subscribers": int(p.get("_nSubscriberCount") or 0),
        "thanks": int(p.get("_nThanksCount") or 0),
    }

# --------------------------------------------------------------------------
# Découverte des endpoints de liste
# GameBanana ne documente pas officiellement son API v11 : on essaie plusieurs
# formes connues et on garde la première qui renvoie des résultats VÉRIFIÉS
# (bon auteur / bonne catégorie). Le gagnant est mémorisé dans config.json.
# --------------------------------------------------------------------------

def q(s):
    return urllib.parse.quote(s, safe="")

MEMBER_CANDIDATES = [
    "/Member/{id}/Submissions?_nPage={page}&_nPerpage=50",
    "/Member/{id}/Submissions?_nPage={page}",
    "/Member/{id}/Subfeed?_nPage={page}&_nPerpage=50",
    "/Mod/Index?_nPage={page}&_nPerpage=50&" + q("_aFilters[Generic_Submitter]") + "={id}",
    "/Mod/ByMember?_aMemberRowIds[]={id}&_csvProperties=_idRow,_sName&_nPage={page}&_nPerpage=50",
]

CATEGORY_CANDIDATES = [
    "/Mod/Index?_nPage={page}&_nPerpage=50&" + q("_aFilters[Generic_Category]") + "={id}&_sSort=Generic_MostDownloaded",
    "/ModCategory/{id}/Subfeed?_nPage={page}&_nPerpage=50&_sSort=popular",
    "/ModCategory/{id}/Subfeed?_nPage={page}",
    "/Mod/ByCategory?_aCategoryRowIds[]={id}&_csvProperties=_idRow,_sName,_aCategory&_nPage={page}&_nPerpage=50",
]


def _check_member(recs, member_id):
    mods = [r for r in recs if is_mod(r)]
    if not mods:
        return False
    subs = [int((r.get("_aSubmitter") or {}).get("_idRow") or -1) for r in mods]
    known = [s for s in subs if s != -1]
    return bool(known) and all(s == member_id for s in known)


def _check_cat(recs, cat_id):
    mods = [r for r in recs if is_mod(r)]
    if not mods:
        return False
    with_cat = [r for r in mods if cat_ids_of(r)]
    return bool(with_cat) and all(cat_id in cat_ids_of(r) for r in with_cat)


def discover(api, cfg, kind, test_id):
    key = f"endpoint_{kind}"
    cands = MEMBER_CANDIDATES if kind == "member" else CATEGORY_CANDIDATES
    check = _check_member if kind == "member" else _check_cat
    if cfg.get(key):
        cands = [cfg[key]] + [c for c in cands if c != cfg[key]]
    for tpl in cands:
        recs = records_of(api.get(tpl.format(id=test_id, page=1)))
        if check(recs, test_id):
            if cfg.get(key) != tpl:
                cfg[key] = tpl
                save_config(cfg)
                log(f"Endpoint {kind} trouvé : {tpl}")
            return tpl
    log(f"Aucun endpoint {kind} ne fonctionne (testé sur {test_id}).")
    return None


def list_ids(api, tpl, the_id, limit, check):
    limit = limit if limit and limit > 0 else 10**9   # 0 = pas de limite
    ids, page = [], 1
    while len(ids) < limit and page <= 500:          # 500 pages = garde-fou anti-boucle
        recs = records_of(api.get(tpl.format(id=the_id, page=page)))
        if not recs or not check(recs, the_id):
            break
        new = [int(r["_idRow"]) for r in recs if is_mod(r) and int(r["_idRow"]) not in ids]
        if not new:
            break
        ids.extend(new)
        page += 1
    return ids[:limit]

# --------------------------------------------------------------------------
# Base de données
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS mods (
  mod_id INTEGER PRIMARY KEY, name TEXT, url TEXT, submitter TEXT, submitter_id INTEGER,
  game TEXT, cat_id INTEGER, cat_name TEXT, supercat_id INTEGER, supercat_name TEXT,
  date_added INTEGER, is_mine INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
  day TEXT, mod_id INTEGER, downloads INTEGER, likes INTEGER, views INTEGER,
  posts INTEGER, subscribers INTEGER, thanks INTEGER,
  PRIMARY KEY (day, mod_id)
);
CREATE TABLE IF NOT EXISTS runs (
  started TEXT PRIMARY KEY, day TEXT, duration_s REAL, requests INTEGER, throttled INTEGER,
  errors INTEGER, max_delay REAL, mods_expected INTEGER, mods_ok INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS category_members (
  day TEXT, cat_id INTEGER, mod_id INTEGER, PRIMARY KEY (day, cat_id, mod_id)
);
"""


def open_db(path):
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    return db


class Writer:
    """Garde les relevés en mémoire et les écrit par paquets de 50 mods,
    en une seule transaction courte : peu d'écritures disque, et la base
    n'est verrouillée que quelques millisecondes à chaque fois."""
    BATCH = 50

    def __init__(self, db, day):
        self.db, self.day = db, day
        self.mods, self.members = [], []

    def add(self, p, mine):
        self.mods.append((p, mine))
        if len(self.mods) >= self.BATCH:
            self.flush()

    def member(self, cat_id, mod_id):
        self.members.append((self.day, cat_id, mod_id))

    def flush(self):
        if not self.mods and not self.members:
            return
        with self.db:  # une transaction = un commit
            for p, mine in self.mods:
                store(self.db, p, self.day, mine)
            self.db.executemany("INSERT OR IGNORE INTO category_members VALUES (?,?,?)", self.members)
        self.mods, self.members = [], []


def store(db, p, day, mine):
    db.execute(
        """INSERT INTO mods VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(mod_id) DO UPDATE SET name=excluded.name, url=excluded.url,
             cat_id=excluded.cat_id, cat_name=excluded.cat_name,
             supercat_id=excluded.supercat_id, supercat_name=excluded.supercat_name,
             is_mine=MAX(mods.is_mine, excluded.is_mine)""",
        (p["mod_id"], p["name"], p["url"], p["submitter"], p["submitter_id"], p["game"],
         p["cat_id"], p["cat_name"], p["supercat_id"], p["supercat_name"], p["date_added"], int(mine)),
    )
    db.execute(
        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
        (day, p["mod_id"], p["downloads"], p["likes"], p["views"], p["posts"], p["subscribers"], p["thanks"]),
    )

# --------------------------------------------------------------------------
# Stockage CSV (version GitHub)
# Les données vivent dans des fichiers texte versionnés par Git :
#   data/mods.csv                         infos des mods (petit, réécrit)
#   data/runs.csv                         bilan de chaque relevé
#   data/snapshots/AAAA/AAAA-MM-JJ.csv    chiffres du jour (1 fichier par jour)
#   data/categories/AAAA/AAAA-MM-JJ.csv   mods de chaque catégorie ce jour-là
# Un fichier du jour n'est plus jamais modifié ensuite : Git ne stocke donc
# que quelques Ko de nouveautés par jour, au lieu d'une base entière.
# Au lancement, tout est rechargé dans une base SQLite en mémoire, et le reste
# du script fonctionne exactement comme en version PC.
# --------------------------------------------------------------------------

DAILY_TABLES = {"snapshots": "snapshots", "category_members": "categories"}
SINGLE_TABLES = {"mods": "mods.csv", "runs": "runs.csv"}


def _columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def _to_csv(db, table, rows):
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(_columns(db, table))
    wr.writerows(["" if v is None else v for v in r] for r in rows)
    return buf.getvalue()


def _write_if_changed(path, text):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    write_atomic(path, text)
    return True


def load_csv(db, data_dir):
    files = [(t, os.path.join(data_dir, f)) for t, f in SINGLE_TABLES.items()]
    for t, sub in DAILY_TABLES.items():
        files += [(t, p) for p in sorted(glob.glob(os.path.join(data_dir, sub, "*", "*.csv")))]
    n = 0
    for table, path in files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            continue
        cols = rows[0]
        known = set(_columns(db, table))
        idx = [i for i, c in enumerate(cols) if c in known]  # tolère une colonne ajoutée/retirée
        names = ",".join(cols[i] for i in idx)
        marks = ",".join("?" * len(idx))
        db.executemany(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})",
                       ([None if r[i] == "" else r[i] for i in idx] for r in rows[1:]))
        n += len(rows) - 1
    db.commit()
    return n


def dump_csv(db, data_dir, days=None):
    """Écrit les fichiers CSV. days=None : tous les jours (export initial)."""
    changed = 0
    for table, fname in SINGLE_TABLES.items():
        order = "mod_id" if table == "mods" else "started"
        rows = db.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        changed += _write_if_changed(os.path.join(data_dir, fname), _to_csv(db, table, rows))
    for table, sub in DAILY_TABLES.items():
        todo = days or [r[0] for r in db.execute(f"SELECT DISTINCT day FROM {table}")]
        for d in todo:
            order = "mod_id" if table == "snapshots" else "cat_id, mod_id"
            rows = db.execute(f"SELECT * FROM {table} WHERE day = ? ORDER BY {order}", (d,)).fetchall()
            if rows:
                path = os.path.join(data_dir, sub, d[:4], f"{d}.csv")
                changed += _write_if_changed(path, _to_csv(db, table, rows))
    return changed


def open_store(cfg):
    if cfg.get("storage") == "csv":
        db = open_db(":memory:")
        n = load_csv(db, os.path.join(HERE, cfg["data_dir"]))
        log(f"{n} lignes chargées depuis {cfg['data_dir']}/")
        return db
    return open_db(os.path.join(HERE, cfg["db_file"]))


def save_store(cfg, db, day):
    if cfg.get("storage") == "csv":
        n = dump_csv(db, os.path.join(HERE, cfg["data_dir"]), [day])
        log(f"{n} fichier(s) CSV mis à jour dans {cfg['data_dir']}/")


# --------------------------------------------------------------------------
# Relevé
# --------------------------------------------------------------------------

def snapshot(cfg, db):
    api = Api(cfg["delay_seconds"])
    day = date.today().isoformat()
    # heure avec fuseau : affichée juste, que le relevé tourne sur ton PC ou chez GitHub (UTC)
    started, t0 = datetime.now().astimezone().isoformat(timespec="seconds"), time.time()
    counts = {"expected": 0, "ok": 0, "problems": []}
    status = "ok"
    w = Writer(db, day)
    try:
        wait_for_network(cfg.get("member_id"))
        _snapshot(cfg, api, db, w, counts)
    except Offline as e:
        status = "hors ligne"
        log(f"ANNULÉ : {e}. Rien n'est perdu : le prochain relevé récupérera les totaux.")
    except Blocked as e:
        status = "bloqué"
        log(f"ARRÊT : {e}. Ce qui a déjà été relevé est conservé. "
            f"Augmente delay_seconds dans config.json et réessaie plus tard.")
    except Exception as e:  # bug imprévu : on garde ce qui est relevé et on le signale
        status = "erreur"
        import traceback
        log("ERREUR inattendue :\n" + traceback.format_exc())
    finally:
        w.flush()  # ce qui a été relevé avant un arrêt est toujours enregistré
        st = api.stats
        if status == "ok" and (st["throttled"] or counts["ok"] < counts["expected"] or counts["problems"]):
            status = "freiné" if st["throttled"] else "incomplet"
        for pb in counts["problems"]:
            log(f"Problème : {pb}")
        db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (started, day, round(time.time() - t0, 1), st["requests"], st["throttled"], st["errors"],
                    st["max_delay"], counts["expected"], counts["ok"], status))
        db.commit()
        save_store(cfg, db, day)
        log(f"Bilan : {counts['ok']}/{counts['expected']} mods relevés, {st['requests']} requêtes, "
            f"{st['throttled']} freinages, pause max {st['max_delay']:.1f} s, "
            f"{time.time() - t0:.0f} s au total -> {status}")
    return status


def _snapshot(cfg, api, db, w, counts):
    member = int(cfg.get("member_id") or 0)

    # 1) Mes mods : config + ceux déjà connus en base (filet de sécurité si
    #    GameBanana ne répond pas aujourd'hui à la liste de mes publications)
    my_ids = {int(x) for x in cfg.get("mod_ids") or []}
    my_ids |= {r[0] for r in db.execute("SELECT mod_id FROM mods WHERE is_mine = 1")}
    if member:
        tpl = discover(api, cfg, "member", member)
        if tpl:
            my_ids |= set(list_ids(api, tpl, member, 500, _check_member))
        else:
            counts["problems"].append("liste de mes mods indisponible : seuls les mods déjà connus sont relevés")
    my_ids = sorted(my_ids)
    if not my_ids:
        counts["problems"].append("aucun mod trouvé. Ajoute leurs IDs dans config.json -> \"mod_ids\": [123, 456]"
                                  " (le nombre à la fin de l'URL gamebanana.com/mods/123)")
        return

    mine = []
    for mid in my_ids:
        counts["expected"] += 1
        p = profile(api, mid)
        if p:
            counts["ok"] += 1
            w.add(p, True)
            mine.append(p)
            log(f"Mon mod : {p['name']} — {p['downloads']} DL, {p['likes']} likes, {p['views']} vues")
    w.flush()

    # 2) Concurrents de chaque catégorie
    cats = {}
    for p in mine:
        if p["cat_id"]:
            cats[p["cat_id"]] = p["cat_name"]
    if cats:
        tpl = discover(api, cfg, "category", next(iter(cats)))
        if tpl:
            for cid, cname in cats.items():
                ids = list_ids(api, tpl, cid, int(cfg.get("max_mods_per_category") or 0), _check_cat)
                eta = len(ids) * float(cfg["delay_seconds"]) / 60
                log(f"Catégorie {cname} ({cid}) : {len(ids)} mods à relever (~{eta:.0f} min)")
                if not ids:
                    counts["problems"].append(f"liste de la catégorie {cname} vide ou illisible")
                counts["expected"] += len([m for m in ids if m not in my_ids])
                for mid in ids:
                    w.member(cid, mid)
                    if mid in my_ids:
                        continue
                    p = profile(api, mid)
                    if p:
                        counts["ok"] += 1
                        w.add(p, False)
        else:
            counts["problems"].append("comparaison par catégorie indisponible : seuls tes mods sont relevés")

# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

PEER_HISTORY_DAYS = 400  # historique des concurrents exporté (le tien : complet)


def export_data(db):
    """Données pour le tableau de bord, en format compact (tableaux, pas
    d'objets) pour que le fichier HTML reste léger malgré les années."""
    db.row_factory = sqlite3.Row
    mods = [dict(r) for r in db.execute("SELECT * FROM mods")]
    since = (date.today() - timedelta(days=PEER_HISTORY_DAYS)).isoformat()
    snaps = [list(r) for r in db.execute(
        """SELECT s.day, s.mod_id, s.downloads, s.likes, s.views FROM snapshots s
           JOIN mods m USING (mod_id) WHERE m.is_mine = 1 OR s.day >= ? ORDER BY s.day""", (since,))]
    last = db.execute("SELECT MAX(day) FROM category_members").fetchone()[0]
    members = [list(r) for r in db.execute(
        "SELECT cat_id, mod_id FROM category_members WHERE day = ?", (last,))]
    try:
        runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 30")]
    except sqlite3.OperationalError:
        runs = []
    db.row_factory = None
    return {"generated": datetime.now().astimezone().isoformat(timespec="minutes"), "mods": mods,
            "snapshots": snaps, "category_members": members, "runs": runs}


def build_dashboard(db, out_path):
    data = export_data(db)
    with open(os.path.join(HERE, "dashboard_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    write_atomic(out_path, tpl.replace("/*__DATA__*/null", payload))
    log(f"Dashboard écrit : {out_path}")

# --------------------------------------------------------------------------
# Démo (fausses données pour voir le rendu)
# --------------------------------------------------------------------------

def demo():
    path = os.path.join(HERE, "demo.db")
    if os.path.exists(path):
        os.remove(path)
    db = open_db(path)
    rnd = random.Random(7)
    today = date.today()
    cats = {(3204, "Ganondorf"): 40, (3208, "Effects"): 30}
    mine_cfg = [("Ganondorf – Demon King Reskin", 3204, "Ganondorf"),
                ("Warlock Punch Star FX", 3208, "Effects")]
    mods = []
    mid = 500000
    for (cid, cname), n in cats.items():
        for i in range(n):
            mid += 1
            age = rnd.randint(30, 1500)
            mods.append({"mod_id": mid, "name": f"{cname} mod #{i+1}", "cat_id": cid, "cat_name": cname,
                         "age": age, "rate": rnd.lognormvariate(1.2, 1.1), "mine": False,
                         "vr": rnd.uniform(2.5, 5), "lr": rnd.uniform(0.02, 0.07)})
    for name, cid, cname in mine_cfg:
        mid += 1
        mods.append({"mod_id": mid, "name": name, "cat_id": cid, "cat_name": cname,
                     "age": rnd.randint(70, 200), "rate": rnd.uniform(6, 14), "mine": True,
                     "vr": 3.4, "lr": 0.055})
    for m in mods:
        added = today - timedelta(days=m["age"])
        p = {"mod_id": m["mod_id"], "name": m["name"], "url": f"https://gamebanana.com/mods/{m['mod_id']}",
             "submitter": "TheMonkGB" if m["mine"] else "someone", "submitter_id": 4254243 if m["mine"] else 1,
             "game": "Super Smash Bros. Ultimate", "cat_id": m["cat_id"], "cat_name": m["cat_name"],
             "supercat_id": 0, "supercat_name": "Skins", "date_added": int(time.mktime(added.timetuple()))}
        dl0 = m["rate"] * (m["age"] - 60)
        for d in range(60, -1, -1):
            day = today - timedelta(days=d)
            if day < added:
                continue
            boost = 3.0 if (m["mine"] and 20 <= d <= 24) else 1.0
            dl0 += max(0.0, rnd.gauss(m["rate"] * boost, m["rate"] * 0.4))
            dl = int(max(dl0, 0))
            p.update(downloads=dl, views=int(dl * m["vr"]), likes=int(dl * m["lr"]), posts=int(dl * 0.01),
                     subscribers=0, thanks=0)
            store(db, p, day.isoformat(), m["mine"])
            db.execute("INSERT OR IGNORE INTO category_members VALUES (?,?,?)",
                       (day.isoformat(), m["cat_id"], m["mod_id"]))
    db.commit()
    build_dashboard(db, os.path.join(HERE, "demo_dashboard.html"))
    db.close()

# --------------------------------------------------------------------------

LOCK_PATH = os.path.join(HERE, "gb_tracker.lock")
LOCK_STALE = 3 * 3600  # un verrou de plus de 3 h vient d'un relevé planté


def already_done_today(db):
    """Le relevé du jour a-t-il déjà réussi ? (complet, même s'il a été freiné)"""
    row = db.execute("SELECT status FROM runs WHERE day = ? AND status IN ('ok', 'freiné') LIMIT 1",
                     (date.today().isoformat(),)).fetchone()
    return row is not None


def acquire_lock():
    """Empêche deux relevés simultanés (tâche planifiée + lancement manuel)."""
    try:
        if time.time() - os.path.getmtime(LOCK_PATH) > LOCK_STALE:
            os.remove(LOCK_PATH)
    except OSError:
        pass
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "run"
    if cmd == "demo":
        demo()
        return
    cfg = load_config()
    out = os.path.join(HERE, cfg["dashboard_file"])
    if cmd == "probe":
        print(json.dumps(Api(0).get(args[1]), indent=2, ensure_ascii=False)[:20000])
        return
    if cmd == "exporter-csv":
        # Convertit une base SQLite (version PC) en fichiers CSV (version GitHub)
        src = args[1] if len(args) > 1 else os.path.join(HERE, "gb_stats.db")
        if not os.path.exists(src):
            log(f"Base introuvable : {src}")
            sys.exit(1)
        db = open_db(src)
        n = dump_csv(db, os.path.join(HERE, cfg["data_dir"]))
        db.close()
        log(f"Export terminé : {n} fichier(s) CSV écrits dans {cfg['data_dir']}/")
        return
    if cmd in ("run", "secours"):
        if not acquire_lock():
            log("Un relevé est déjà en cours : celui-ci est annulé.")
            return
        status = "ok"
        try:
            db = open_store(cfg)
            if cmd == "secours" and already_done_today(db):
                log("=== Relevé de secours : le relevé du jour a déjà réussi, rien à faire ===")
                build_dashboard(db, out)  # le tableau de bord reste publiable tel quel
                db.close()
                return
            log("=== Relevé de secours ===" if cmd == "secours" else "=== Relevé du jour ===")
            status = snapshot(cfg, db)
            build_dashboard(db, out)
            db.close()
        finally:
            os.remove(LOCK_PATH)
        if status in ("bloqué", "hors ligne", "erreur"):
            sys.exit(2)  # code d'erreur : GitHub t'envoie un e-mail d'échec
    elif cmd == "dashboard":
        db = open_store(cfg)
        build_dashboard(db, out)
        db.close()
    else:
        print(__doc__)


if __name__ == "__main__":
    # Lancé par la tâche planifiée via pythonw, il n'y a pas de console :
    # toute erreur, même avant le début du relevé (config.json mal formé...),
    # doit finir dans le journal pour ne pas passer inaperçue.
    try:
        main()
    except Exception:
        import traceback
        log("ERREUR au lancement :\n" + traceback.format_exc())
        sys.exit(1)
