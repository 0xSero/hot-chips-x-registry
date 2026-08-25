#!/usr/bin/env python3
"""Read-only Hot Chips monitor using X API recent search.

The monitor never posts, follows, likes, refreshes OAuth tokens, or opens a
browser session. It borrows the current access token from an owner-only SQLite
database (or X_BEARER_TOKEN), keeps per-query cursors, and writes a local index.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
OUTPUT_DIR = ROOT / "docs"
DB_PATH = STATE_DIR / "hot-chips.sqlite3"


COMPANIES = {
    "Cerebras": ("cerebras", "wafer scale", "wse"),
    "OpenAI": ("openai",),
    "NVIDIA": ("nvidia", "rubin", "bluefield", "spectrum-x", "lpu accelerator"),
    "AMD": ("amd", "mi400", "versal rf"),
    "Intel": ("intel", "crescent island", "diamond rapids", "wildcat lake"),
    "Google": ("google", "tpu"),
    "Microsoft": ("microsoft", "maia 200", "maia-200"),
    "Meta": ("meta", "custom ai silicon"),
    "Broadcom": ("broadcom", "thor ultra"),
    "SambaNova": ("sambanova", "sn50", "rdu"),
    "Samsung": ("samsung", "lpddr5x-pim", "xcena"),
    "Arm": ("arm agi", "arm-based"),
    "Fujitsu": ("fujitsu", "monaka"),
}

OFFICIAL_HANDLES = {
    "cerebras", "openai", "nvidia", "amd", "intel", "hotchipsorg", "google",
    "microsoft", "meta", "broadcom", "samsungdsglobal",
}

REPORTER_HANDLES = {
    "semianalysis_", "dylan522p", "servethehome", "iancutress",
    "patrickmoorhead", "patrick1kennedy", "yahoofinance",
}

PRODUCTS = {
    "Cerebras rack-scale": ("rack-scale architecture", "rack scale architecture"),
    "WSE": ("wafer scale engine", "wse"),
    "Jalapeño": ("jalapeño", "jalapeno"),
    "Rubin": ("rubin gpu", "nvidia rubin"),
    "MI400": ("mi400",),
    "Crescent Island": ("crescent island",),
    "BlueField-4": ("bluefield-4", "bluefield 4"),
    "Spectrum-X": ("spectrum-x", "spectrum x"),
    "Thor Ultra": ("thor ultra",),
    "LPDDR5X-PIM": ("lpddr5x-pim", "lpddr5x pim"),
    "XCENA MX1": ("xcena mx1",),
    "MAIA 200": ("maia 200", "maia-200"),
    "SN50 RDU": ("sn50",),
    "TPU Gen 8": ("eighth generation tpu", "8th generation tpu"),
}

HARDWARE_TERMS = (
    "chip", "asic", "gpu", "cpu", "accelerator", "silicon", "wafer", "memory",
    "hbm", "interconnect", "nic", "processor", "architecture", "tokens per second",
    "throughput", "latency", "power", "watt", "rack", "dataflow", "inference",
)

LAUNCH_TERMS = (
    "launch", "unveil", "announce", "reveal", "introduce", "first", "new",
    "benchmark", "spec", "architecture", "available", "ship", "deploy",
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    x = config.get("x", {})
    lanes = x.get("lanes", [])
    if not lanes:
        raise ValueError("config has no X search lanes")
    if not 10 <= int(x.get("results_per_request", 0)) <= 100:
        raise ValueError("results_per_request must be from 10 to 100")
    for lane in lanes:
        query = lane.get("query", "")
        if not lane.get("name") or not query:
            raise ValueError("every lane needs a name and query")
        if len(query) > 512:
            raise ValueError(f"query {lane['name']} exceeds X recent-search limit")
    return config


def ensure_private_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.chmod(0o755)


def connect_db() -> sqlite3.Connection:
    ensure_private_dirs()
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS posts (
          post_id TEXT PRIMARY KEY,
          text TEXT NOT NULL,
          author_id TEXT,
          username TEXT,
          author_name TEXT,
          author_verified INTEGER NOT NULL DEFAULT 0,
          created_at TEXT,
          conversation_id TEXT,
          lang TEXT,
          reply_count INTEGER NOT NULL DEFAULT 0,
          retweet_count INTEGER NOT NULL DEFAULT 0,
          like_count INTEGER NOT NULL DEFAULT 0,
          quote_count INTEGER NOT NULL DEFAULT 0,
          bookmark_count INTEGER NOT NULL DEFAULT 0,
          impression_count INTEGER NOT NULL DEFAULT 0,
          post_url TEXT NOT NULL,
          external_urls_json TEXT NOT NULL DEFAULT '[]',
          companies_json TEXT NOT NULL DEFAULT '[]',
          products_json TEXT NOT NULL DEFAULT '[]',
          relevance_score INTEGER NOT NULL DEFAULT 0,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS query_hits (
          post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
          lane TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          PRIMARY KEY (post_id, lane)
        );
        CREATE TABLE IF NOT EXISTS cursors (
          lane TEXT PRIMARY KEY,
          since_id TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_usage (
          day_utc TEXT PRIMARY KEY,
          resources INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          resources INTEGER NOT NULL DEFAULT 0,
          inserted INTEGER NOT NULL DEFAULT 0,
          updated INTEGER NOT NULL DEFAULT 0,
          detail TEXT
        );
        """
    )
    connection.commit()
    DB_PATH.chmod(0o600)
    return connection


def token_from_sqlite(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("token_sqlite must be an absolute path")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT access_token FROM oauth_tokens WHERE key = 'bot'"
        ).fetchone()
    finally:
        connection.close()
    if not row or not isinstance(row[0], str) or not row[0]:
        raise RuntimeError("no current X access token in the configured SQLite source")
    return row[0]


def load_access_token(config: dict) -> str:
    direct = os.environ.get("X_BEARER_TOKEN", "").strip()
    if direct:
        return direct
    source = config["x"].get("token_sqlite")
    if not source:
        raise RuntimeError("set X_BEARER_TOKEN or x.token_sqlite")
    return token_from_sqlite(Path(source))


def api_get(url: str, token: str) -> tuple[dict, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "hot-chips-read-only-monitor/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
            headers = {key.lower(): value for key, value in response.headers.items()}
            return payload, headers
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        if error.code == 401:
            raise RuntimeError(
                "X access token is stale; the dedicated bot must refresh it on its next poll"
            ) from None
        if error.code == 429:
            reset = error.headers.get("x-rate-limit-reset", "unknown")
            raise RuntimeError(f"X rate limit reached; reset={reset}") from None
        raise RuntimeError(f"X API returned HTTP {error.code}: {body[:800]}") from None


def tag_matches(text: str, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    lower = text.lower()
    return [label for label, needles in mapping.items() if any(n in lower for n in needles)]


def relevance_score(post: dict, user: dict | None = None) -> tuple[int, list[str], list[str]]:
    text = post.get("text", "")
    lower = text.lower()
    companies = tag_matches(text, COMPANIES)
    products = tag_matches(text, PRODUCTS)
    jalapeno_context = any(term in lower for term in (
        "openai", "ai chip", "custom chip", "in-house chip", "asic", "inference", "accelerator", "semiconductor",
        "nvidia", "broadcom", "tokens per", "throughput", "latency",
    ))
    if "Jalapeño" in products and not jalapeno_context:
        products.remove("Jalapeño")
    score = 0
    if "hot chips" in lower or "#hotchips" in lower or "#hc38" in lower:
        score += 10
    score += min(28, len(companies) * 9)
    score += min(48, len(products) * 16)
    if any(term in lower for term in HARDWARE_TERMS):
        score += 12
    if any(term in lower for term in LAUNCH_TERMS):
        score += 8
    if "cerebras" in lower:
        score += 14
    if ("jalapeño" in lower or "jalapeno" in lower) and jalapeno_context:
        score += 14
    if user and user.get("verified"):
        score += 5
    username = str((user or {}).get("username") or "").lower()
    if username in OFFICIAL_HANDLES:
        score += 24
    elif username in REPORTER_HANDLES:
        score += 12
    metrics = post.get("public_metrics") or {}
    engagement = sum(int(metrics.get(key, 0) or 0) for key in ("like_count", "retweet_count", "quote_count"))
    score += min(12, int(math.log2(engagement + 1) * 2))
    if post.get("in_reply_to_user_id") or lower.startswith("@"): 
        score -= 4
    return max(0, score), companies, products


def external_urls(post: dict) -> list[str]:
    urls: list[str] = []
    for entry in ((post.get("entities") or {}).get("urls") or []):
        value = entry.get("unwound_url") or entry.get("expanded_url")
        if isinstance(value, str) and value.startswith(("http://", "https://")) and "x.com/" not in value:
            urls.append(value)
    return list(dict.fromkeys(urls))


def upsert_posts(connection: sqlite3.Connection, payload: dict, lane: str) -> tuple[int, int]:
    now = iso_now()
    users = {item["id"]: item for item in payload.get("includes", {}).get("users", [])}
    inserted = updated = 0
    for post in payload.get("data", []) or []:
        post_id = post["id"]
        user = users.get(post.get("author_id"), {})
        username = user.get("username")
        url = f"https://x.com/{username or 'i/web'}/status/{post_id}"
        score, companies, products = relevance_score(post, user)
        metrics = post.get("public_metrics") or {}
        exists = connection.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        values = (
            post_id, post.get("text", ""), post.get("author_id"), username, user.get("name"),
            int(bool(user.get("verified"))), post.get("created_at"), post.get("conversation_id"),
            post.get("lang"), int(metrics.get("reply_count", 0) or 0),
            int(metrics.get("retweet_count", 0) or 0), int(metrics.get("like_count", 0) or 0),
            int(metrics.get("quote_count", 0) or 0), int(metrics.get("bookmark_count", 0) or 0),
            int(metrics.get("impression_count", 0) or 0), url,
            json.dumps(external_urls(post), ensure_ascii=False),
            json.dumps(companies, ensure_ascii=False), json.dumps(products, ensure_ascii=False),
            score, now, now, json.dumps({"post": post, "user": user}, ensure_ascii=False),
        )
        connection.execute(
            """
            INSERT INTO posts (
              post_id, text, author_id, username, author_name, author_verified, created_at,
              conversation_id, lang, reply_count, retweet_count, like_count, quote_count,
              bookmark_count, impression_count, post_url, external_urls_json, companies_json,
              products_json, relevance_score, first_seen_at, last_seen_at, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(post_id) DO UPDATE SET
              text=excluded.text, username=excluded.username, author_name=excluded.author_name,
              author_verified=excluded.author_verified, reply_count=excluded.reply_count,
              retweet_count=excluded.retweet_count, like_count=excluded.like_count,
              quote_count=excluded.quote_count, bookmark_count=excluded.bookmark_count,
              impression_count=excluded.impression_count, external_urls_json=excluded.external_urls_json,
              companies_json=excluded.companies_json, products_json=excluded.products_json,
              relevance_score=excluded.relevance_score, last_seen_at=excluded.last_seen_at,
              raw_json=excluded.raw_json
            """,
            values,
        )
        connection.execute(
            "INSERT OR IGNORE INTO query_hits (post_id, lane, first_seen_at) VALUES (?, ?, ?)",
            (post_id, lane, now),
        )
        if exists:
            updated += 1
        else:
            inserted += 1
    return inserted, updated


def get_daily_usage(connection: sqlite3.Connection) -> int:
    day = utc_now().date().isoformat()
    row = connection.execute("SELECT resources FROM daily_usage WHERE day_utc = ?", (day,)).fetchone()
    return int(row[0]) if row else 0


def add_daily_usage(connection: sqlite3.Connection, count: int) -> None:
    day = utc_now().date().isoformat()
    connection.execute(
        """
        INSERT INTO daily_usage (day_utc, resources) VALUES (?, ?)
        ON CONFLICT(day_utc) DO UPDATE SET resources = resources + excluded.resources
        """,
        (day, count),
    )


def search_url(config: dict, lane: dict, since_id: str | None, allowed: int) -> str:
    limit = min(int(config["x"]["results_per_request"]), allowed)
    if limit < 10:
        raise ValueError("X recent search requires room for at least 10 results")
    params = {
        "query": lane["query"],
        "max_results": str(limit),
        "sort_order": "recency",
        "tweet.fields": "author_id,created_at,public_metrics,entities,conversation_id,lang,in_reply_to_user_id",
        "expansions": "author_id",
        "user.fields": "id,name,username,verified,public_metrics",
    }
    if since_id:
        params["since_id"] = since_id
    else:
        params["start_time"] = config["conference"]["bootstrap_start_time"]
    return f"{config['x']['api_base']}/2/tweets/search/recent?{urllib.parse.urlencode(params)}"


def collect(config: dict, connection: sqlite3.Connection) -> dict:
    token = load_access_token(config)
    ceiling = int(config["x"]["daily_resource_ceiling"])
    run_started = iso_now()
    cursor = connection.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (run_started,)
    )
    run_id = cursor.lastrowid
    connection.commit()
    summary = {"run_id": run_id, "resources": 0, "inserted": 0, "updated": 0, "lanes": []}
    try:
        for lane in config["x"]["lanes"]:
            used = get_daily_usage(connection)
            remaining = ceiling - used
            if remaining < 10:
                summary["lanes"].append({"name": lane["name"], "status": "daily ceiling reached"})
                continue
            row = connection.execute("SELECT since_id FROM cursors WHERE lane = ?", (lane["name"],)).fetchone()
            since_id = row[0] if row else None
            url = search_url(config, lane, since_id, remaining)
            payload, headers = api_get(url, token)
            count = int(payload.get("meta", {}).get("result_count", 0) or 0)
            inserted, updated = upsert_posts(connection, payload, lane["name"])
            add_daily_usage(connection, count)
            newest_id = payload.get("meta", {}).get("newest_id")
            if newest_id:
                connection.execute(
                    """
                    INSERT INTO cursors (lane, since_id, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(lane) DO UPDATE SET since_id=excluded.since_id, updated_at=excluded.updated_at
                    """,
                    (lane["name"], newest_id, iso_now()),
                )
            summary["resources"] += count
            summary["inserted"] += inserted
            summary["updated"] += updated
            summary["lanes"].append({
                "name": lane["name"], "status": "ok", "resources": count,
                "inserted": inserted, "updated": updated, "since_id": newest_id or since_id,
                "rate_remaining": headers.get("x-rate-limit-remaining"),
            })
            connection.commit()
        connection.execute(
            "UPDATE runs SET finished_at=?, status='ok', resources=?, inserted=?, updated=?, detail=? WHERE run_id=?",
            (iso_now(), summary["resources"], summary["inserted"], summary["updated"],
             json.dumps(summary["lanes"]), run_id),
        )
        connection.commit()
        return summary
    except Exception as error:
        connection.execute(
            "UPDATE runs SET finished_at=?, status='error', resources=?, inserted=?, updated=?, detail=? WHERE run_id=?",
            (iso_now(), summary["resources"], summary["inserted"], summary["updated"], str(error), run_id),
        )
        connection.commit()
        raise


def rows_for_export(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT p.*, GROUP_CONCAT(q.lane) AS lanes
        FROM posts p LEFT JOIN query_hits q ON q.post_id = p.post_id
        GROUP BY p.post_id
        ORDER BY p.created_at DESC, p.post_id DESC
        """
    ).fetchall()
    exported = []
    for row in rows:
        item = dict(row)
        for key in ("external_urls_json", "companies_json", "products_json"):
            item[key.removesuffix("_json")] = json.loads(item.pop(key))
        item["lanes"] = sorted((item.get("lanes") or "").split(","))
        username = str(item.get("username") or "").lower()
        item["source_tier"] = (
            "official" if username in OFFICIAL_HANDLES else
            "reporter" if username in REPORTER_HANDLES else
            "community"
        )
        item.pop("raw_json", None)
        exported.append(item)
    return exported


def rescore_all(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT post_id, raw_json FROM posts").fetchall()
    for row in rows:
        raw = json.loads(row["raw_json"])
        score, companies, products = relevance_score(raw.get("post", {}), raw.get("user", {}))
        connection.execute(
            "UPDATE posts SET relevance_score=?, companies_json=?, products_json=? WHERE post_id=?",
            (score, json.dumps(companies, ensure_ascii=False), json.dumps(products, ensure_ascii=False), row["post_id"]),
        )
    connection.commit()


def atomic_write(path: Path, content: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=OUTPUT_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_index(posts: list[dict], connection: sqlite3.Connection, config: dict) -> str:
    last_run = connection.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    usage = get_daily_usage(connection)
    ceiling = int(config["x"]["daily_resource_ceiling"])
    high_signal = sum(1 for post in posts if post["relevance_score"] >= 35)
    official = sum(1 for post in posts if post["source_tier"] == "official")
    reporters = sum(1 for post in posts if post["source_tier"] == "reporter")
    cutoff = utc_now() - dt.timedelta(hours=1)
    last_hour = 0
    for post in posts:
        try:
            created = dt.datetime.fromisoformat((post.get("created_at") or "").replace("Z", "+00:00"))
            last_hour += int(created >= cutoff)
        except ValueError:
            pass
    companies = sorted({company for post in posts for company in post["companies"]})
    products = sorted({product for post in posts for product in post["products"]})
    generated = iso_now()
    options = lambda values: "".join(
        f'<option value="{html.escape(value)}">{html.escape(value)}</option>' for value in values
    )
    last_status = last_run["status"] if last_run else "not run"
    cards = []
    for post in posts:
        entities = post["companies"] + post["products"]
        tags = "".join(f'<span class="tag">{html.escape(tag)}</span>' for tag in entities)
        searchable = " ".join([
            post.get("text") or "", post.get("username") or "", post.get("author_name") or "",
            *post["companies"], *post["products"],
        ]).lower()
        engagement = post["like_count"] + post["retweet_count"] + post["quote_count"]
        created = (post.get("created_at") or "").replace("T", " ").replace(".000Z", " UTC")
        cards.append(f'''<article class="post-card" data-search="{html.escape(searchable, quote=True)}"
          data-company="{html.escape('|'.join(post['companies']), quote=True)}" data-product="{html.escape('|'.join(post['products']), quote=True)}"
          data-source="{html.escape(post['source_tier'], quote=True)}" data-score="{post['relevance_score']}"
          data-created="{html.escape(post.get('created_at') or '', quote=True)}" data-engagement="{engagement}">
          <div class="post-meta"><span class="signal">SIGNAL {post['relevance_score']}</span><span>{html.escape(created)}</span><span>{html.escape(post['source_tier'].upper())}</span></div>
          <div class="post-author"><strong>{html.escape(post.get('author_name') or 'Unknown author')}{' ✓' if post['author_verified'] else ''}</strong><span>@{html.escape(post.get('username') or 'unknown')}</span></div>
          <p class="post-text">{html.escape(post.get('text') or '')}</p>
          <div class="post-bottom"><div class="tags">{tags}</div><div class="post-actions"><span>♥ {post['like_count']} &nbsp; ↻ {post['retweet_count']} &nbsp; ◇ {post['quote_count']}</span><a href="{html.escape(post['post_url'], quote=True)}" target="_blank" rel="noreferrer">View on X ↗</a></div></div>
        </article>''')
    cards_html = "".join(cards)
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="120">
  <title>Hot Chips X Registry</title>
  <style>
    :root {{ --black:#000; --white:#fff; --orange:#ff5a1f; --line:rgba(0,0,0,.12); --muted:rgba(0,0,0,.58); --soft:rgba(0,0,0,.035); }}
    * {{ box-sizing:border-box; }}
    html {{ background:var(--white); color:var(--black); }}
    body {{ margin:0; font-family:"Helvetica Neue",Helvetica,sans-serif; font-size:14px; }}
    a {{ color:inherit; }}
    .container {{ width:min(1120px,100%); margin:auto; padding-inline:24px; }}
    nav {{ height:68px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--line); }}
    .brand {{ display:flex; align-items:center; gap:10px; font-weight:600; letter-spacing:-.02em; }}
    .brand-mark {{ width:12px; height:12px; background:var(--orange); }}
    .nav-meta {{ color:var(--muted); font:11px "SFMono-Regular",monospace; }}
    .hero {{ padding-block:92px 78px; border-bottom:1px solid var(--line); }}
    .kicker {{ color:var(--orange); font:11px "SFMono-Regular",monospace; text-transform:uppercase; letter-spacing:.1em; margin-bottom:24px; }}
    h1 {{ font-size:clamp(48px,8vw,88px); line-height:.98; letter-spacing:-.065em; margin:0; font-weight:600; max-width:900px; }}
    .subtitle {{ color:var(--muted); max-width:650px; margin:28px 0 0; font-size:18px; line-height:1.6; }}
    .stats-section {{ padding-block:64px; border-bottom:1px solid var(--line); }}
    .section-label {{ font:11px "SFMono-Regular",monospace; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; margin-bottom:24px; }}
    .stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }}
    .stat {{ border:1px solid var(--line); border-radius:10px; padding:24px; min-height:118px; display:flex; flex-direction:column; justify-content:space-between; }}
    .stat strong {{ display:block; font:500 28px/1 "SFMono-Regular",monospace; }}
    .stat.highlight strong {{ color:var(--orange); }}
    .stat span {{ color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.1em; }}
    .explore {{ padding-block:80px; }}
    .explore-head {{ display:flex; justify-content:space-between; align-items:end; gap:24px; margin-bottom:32px; }}
    h2 {{ margin:0; font-size:36px; letter-spacing:-.04em; font-weight:600; }}
    .result-count {{ color:var(--muted); font:11px "SFMono-Regular",monospace; }}
    .filters {{ display:grid; grid-template-columns:2fr repeat(2,1fr); gap:12px; margin-bottom:48px; }}
    .filters .wide {{ grid-column:1/-1; }}
    input,select {{ appearance:none; width:100%; background:var(--white); color:var(--black); border:1px solid var(--line); border-radius:8px; padding:14px 15px; font:12px "SFMono-Regular",monospace; }}
    select {{ background-image:linear-gradient(45deg,transparent 50%,var(--orange) 50%),linear-gradient(135deg,var(--orange) 50%,transparent 50%); background-position:calc(100% - 14px) 50%,calc(100% - 9px) 50%; background-size:5px 5px; background-repeat:no-repeat; padding-right:25px; }}
    input:focus,select:focus,a:focus-visible {{ outline:2px solid var(--orange); outline-offset:1px; }}
    .registry {{ display:grid; gap:20px; }}
    .post-card {{ border:1px solid var(--line); border-radius:12px; padding:28px; background:var(--white); }}
    .post-card[hidden] {{ display:none; }}
    .post-meta {{ display:flex; flex-wrap:wrap; gap:18px; color:var(--muted); font:10px "SFMono-Regular",monospace; text-transform:uppercase; letter-spacing:.07em; margin-bottom:24px; }}
    .signal {{ color:var(--orange); }}
    .post-author {{ display:flex; align-items:baseline; gap:10px; margin-bottom:18px; }}
    .post-author strong {{ font-size:15px; }} .post-author span {{ color:var(--muted); font:11px "SFMono-Regular",monospace; }}
    .post-text {{ white-space:pre-wrap; overflow-wrap:anywhere; font-size:17px; line-height:1.65; max-width:850px; margin:0; }}
    .post-bottom {{ display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-top:28px; padding-top:20px; border-top:1px solid var(--line); }}
    .tags {{ display:flex; flex-wrap:wrap; gap:4px; }}
    .tag {{ border:1px solid var(--line); border-radius:999px; padding:5px 8px; font:9px "SFMono-Regular",monospace; text-transform:uppercase; }}
    .post-actions {{ display:flex; align-items:center; gap:20px; white-space:nowrap; color:var(--muted); font:10px "SFMono-Regular",monospace; }}
    .post-actions a {{ color:var(--orange); text-decoration:none; }} .post-actions a:hover {{ text-decoration:underline; }}
    .empty {{ display:none; text-align:center; color:var(--muted); padding:72px 0; }}
    .load-more {{ display:block; margin:36px auto 0; border:1px solid var(--black); border-radius:8px; background:var(--black); color:var(--white); padding:13px 22px; font:12px "SFMono-Regular",monospace; cursor:pointer; }}
    .load-more:hover {{ background:var(--orange); border-color:var(--orange); }}
    footer {{ border-top:1px solid var(--line); padding-block:32px 48px; display:flex; justify-content:space-between; gap:18px; color:var(--muted); font:10px/1.5 "SFMono-Regular",monospace; }}
    footer a {{ color:var(--orange); }}
    @media(max-width:760px) {{ .hero {{ padding-block:64px; }} .stats {{ grid-template-columns:repeat(2,1fr); }} .filters {{ grid-template-columns:1fr; }} .filters .wide {{ grid-column:auto; }} .post-bottom,.explore-head {{ align-items:flex-start; flex-direction:column; }} .post-actions {{ width:100%; justify-content:space-between; }} footer {{ flex-direction:column; }} }}
  </style>
</head>
<body>
  <nav class="container"><div class="brand"><span class="brand-mark"></span>Hot Chips Registry</div><div class="nav-meta">UPDATED {html.escape(generated)}</div></nav>
  <section class="hero container"><div class="kicker">HC38 / Public X Index / Collector {html.escape(last_status.upper())}</div><h1>Hardware launches, indexed.</h1>
    <p class="subtitle">A clean, read-only registry of chip announcements, architecture claims, benchmarks, and conference reporting from Hot Chips 2026.</p></section>
  <section class="stats-section"><div class="container"><div class="section-label">Registry stats</div><div class="stats" aria-label="Registry statistics">
    <div class="stat highlight"><strong>{len(posts)}</strong><span>indexed posts</span></div>
    <div class="stat"><strong>{last_hour}</strong><span>last 60 minutes</span></div>
    <div class="stat"><strong>{high_signal}</strong><span>signal score ≥35</span></div>
    <div class="stat"><strong>{official}</strong><span>official sources</span></div>
    <div class="stat"><strong>{reporters}</strong><span>reporter sources</span></div>
    <div class="stat"><strong>{usage}/{ceiling}</strong><span>API reads today</span></div>
  </div></div></section>
  <section class="explore container"><div class="explore-head"><div><div class="section-label">Explore</div><h2>Post registry</h2></div><div class="result-count" id="count"></div></div>
  <div class="filters" aria-label="Registry filters">
    <input class="wide" id="search" type="search" placeholder="Search post text, account, company, product…" aria-label="Search registry">
    <select id="company" aria-label="Company"><option value="">All companies</option>{options(companies)}</select>
    <select id="product" aria-label="Product"><option value="">All products</option>{options(products)}</select>
    <select id="source" aria-label="Source tier"><option value="">All sources</option><option value="official">Official</option><option value="reporter">Reporter</option><option value="community">Community</option></select>
    <select id="threshold" aria-label="Minimum score"><option value="0">Any score</option><option value="20">Score ≥20</option><option value="35" selected>Score ≥35</option><option value="55">Score ≥55</option></select>
    <select id="sort" aria-label="Sort"><option value="latest">Newest first</option><option value="signal">Highest signal</option><option value="engagement">Most engaged</option></select>
  </div><div class="registry" id="registry">{cards_html}</div><div class="empty" id="empty">No posts match these filters.</div><button class="load-more" id="more" type="button">Load 25 more</button></section>
  <footer class="container"><span>Official program: <a href="{html.escape(config['conference']['official_program_url'])}">hotchips.org</a> · public posts stay attributed to their X authors</span><span>Auto-refresh 120s · JSON + JSONL exports included</span></footer>
<script>
  const controls=['search','company','product','source','threshold','sort'].map(id=>document.getElementById(id));
  const [search,company,product,source,threshold,sort]=controls, count=document.getElementById('count'), registry=document.getElementById('registry'), empty=document.getElementById('empty'), more=document.getElementById('more');
  const cards=[...registry.querySelectorAll('.post-card')]; let limit=25;
  function render() {{
    const q=search.value.trim().toLowerCase(), c=company.value, pr=product.value, sr=source.value, min=Number(threshold.value);
    const visible=cards.filter(card=>Number(card.dataset.score)>=min&&(!c||card.dataset.company.split('|').includes(c))&&(!pr||card.dataset.product.split('|').includes(pr))&&(!sr||card.dataset.source===sr)&&(!q||card.dataset.search.includes(q)));
    visible.sort((a,b)=>sort.value==='signal'?(Number(b.dataset.score)-Number(a.dataset.score)||b.dataset.created.localeCompare(a.dataset.created)):sort.value==='engagement'?(Number(b.dataset.engagement)-Number(a.dataset.engagement)||b.dataset.created.localeCompare(a.dataset.created)):b.dataset.created.localeCompare(a.dataset.created));
    visible.forEach(card=>registry.appendChild(card)); cards.forEach(card=>card.hidden=true); visible.slice(0,limit).forEach(card=>card.hidden=false);
    count.textContent=`${{visible.length}} of ${{cards.length}} posts`; empty.style.display=visible.length?'none':'block'; more.hidden=visible.length<=limit;
  }}
  controls.forEach(el=>el.addEventListener('input',()=>{{limit=25;render();}})); more.addEventListener('click',()=>{{limit+=25;render();}}); render();
</script></body></html>'''


def export_all(connection: sqlite3.Connection, config: dict) -> dict:
    rescore_all(connection)
    posts = rows_for_export(connection)
    atomic_write(OUTPUT_DIR / "posts.json", json.dumps(posts, ensure_ascii=False, indent=2) + "\n")
    atomic_write(OUTPUT_DIR / "posts.jsonl", "".join(json.dumps(post, ensure_ascii=False) + "\n" for post in posts))
    atomic_write(OUTPUT_DIR / "index.html", render_index(posts, connection, config))
    return {"posts": len(posts), "index": str(OUTPUT_DIR / "index.html")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Hot Chips X API monitor")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--index-only", action="store_true", help="rebuild exports without calling X")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    connection = connect_db()
    try:
        summary = None if args.index_only else collect(config, connection)
        exported = export_all(connection, config)
        print(json.dumps({"collection": summary, "export": exported}, ensure_ascii=False, indent=2))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"hot-chips-monitor: {error}", file=sys.stderr)
        raise SystemExit(1)
