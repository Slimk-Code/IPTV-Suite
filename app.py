#!/usr/bin/env python3
"""
MAC Portal to M3U Converter - Flask Web App v2.4
Merged with fairy-root/mac-to-m3u optimizations:
  • Fast live path via get_all_channels (single call)
  • server/load.php genre fallback
  • Parallel VOD page fetching (ThreadPool)
  • Base64 series cmd + full episode expansion
  • localhost proxy fix
Flow:
   1. /api/check        — fast auth, returns account info
   2. /api/categories   — fetch raw category/genre names (fast, no channels)
   3. /api/category-count  — fetch page-1 count for a single category
   4. /api/enrich-counts   — batch page-1 counts for all categories
   5. /api/convert      — download selected channels as M3U or save as JSON playlist
"""

import hashlib, json, math, re, requests, base64, os, uuid, threading, time, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_file, Response, stream_with_context, redirect
from urllib.parse import quote, urlparse
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

MAG_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) "
          "AppleWebKit/533.3 (KHTML, like Gecko) "
          "MAG200 stbapp ver: 2 rev: 250 Safari/533.3")

# ─── Auth ──────────────────────────────────────────────────────────────────────

def get_token(portal_url, mac, timeout=30):
    parsed      = urlparse(portal_url)
    parsed_path = parsed.path
    host        = parsed.hostname
    port        = parsed.port or 80

    if parsed_path.endswith("c"):    parsed_path = parsed_path[:-1]
    if parsed_path.endswith("c/"):   parsed_path = parsed_path[:-2]

    base_url = f"http://{host}:{port}"
    headers  = {"User-Agent": MAG_UA, "Accept-Encoding": "identity",
                 "Accept": "*/*", "Connection": "keep-alive"}

    portal_type = None
    for path, pt in [("/c/version.js", "portal.php"),
                     ("/stalker_portal/c/version.js", "stalker_portal/server/load.php")]:
        try:
            r = requests.get(f"{base_url}{path}", headers=headers, timeout=8)
            if r.status_code == 200:
                portal_type = pt; break
        except: pass
    if not portal_type:
        portal_type = "portal.php"

    if parsed_path and not parsed_path.startswith("/"): parsed_path = "/" + parsed_path
    elif not parsed_path: parsed_path = "/"
    base_url = f"http://{host}:{port}{parsed_path}"
    if "stalker_portal/" in base_url and "stalker_portal/" in portal_type:
        base_url = base_url.replace("stalker_portal/", "")

    sn_full      = hashlib.md5(mac.encode()).hexdigest().upper()
    sn           = sn_full[0:13]
    device_id    = hashlib.sha256(sn.encode()).hexdigest().upper()
    device_id2   = hashlib.sha256(mac.encode()).hexdigest().upper()
    hw_version_2 = hashlib.sha1(mac.encode()).hexdigest()

    cookies = {
        "adid": hw_version_2, "debug": "1",
        "device_id2": device_id2, "device_id": device_id,
        "hw_version": "1.7-BD-00", "mac": mac,
        "sn": sn, "stb_lang": "en", "timezone": "America/Los_Angeles",
    }

    session = requests.Session()
    hs_url  = f"{base_url}{portal_type}?action=handshake&type=stb&token=&JsHttpRequest=1-xml"
    try:
        r = session.get(hs_url, cookies=cookies, headers=headers, timeout=timeout)
        r.raise_for_status()
        js    = r.json().get("js", {})
        token = js.get("token")
        token_random = js.get("random")
        if not token: return None
    except: return None

    return {
        "session": session, "base_url": base_url, "portal_type": portal_type,
        "token": token, "token_random": token_random,
        "mac": mac, "sn": sn,
        "device_id": device_id, "device_id2": device_id2, "hw_version_2": hw_version_2,
    }


def auth_session(ctx):
    sess = ctx["session"]
    sn   = ctx["sn"]
    mac  = ctx["mac"]
    token        = ctx["token"]
    token_random = ctx["token_random"]

    snmac = f"{sn}{mac}"
    sig   = hashlib.sha256(snmac.encode()).hexdigest().upper()
    if token_random:
        sig = hashlib.sha256(str(token_random).encode()).hexdigest().upper()

    sess.cookies.update({
        "adid": ctx["hw_version_2"], "debug": "1",
        "device_id2": ctx["device_id2"], "device_id": ctx["device_id"],
        "hw_version": "1.7-BD-00", "mac": mac,
        "sn": sn, "stb_lang": "en", "timezone": "America/Los_Angeles",
        "token": token,
    })
    sess.headers.update({
        "User-Agent": MAG_UA, "Accept-Encoding": "identity",
        "Accept": "*/*", "Connection": "keep-alive",
        "Authorization": f"Bearer {token}",
    })
    if token_random:
        sess.headers.update({"X-Random": str(token_random)})
    try:
        url = (f"{ctx['base_url']}{ctx['portal_type']}?type=stb&action=get_profile"
               f"&sn={sn}&device_id={ctx['device_id2']}&device_id2={ctx['device_id2']}"
               f"&sig={sig}&JsHttpRequest=1-xml")
        r = sess.get(url, timeout=10)
        return r.json().get("js", {})
    except: return {}


def get_main_info(ctx):
    try:
        url = f"{ctx['base_url']}{ctx['portal_type']}?type=account_info&action=get_main_info&JsHttpRequest=1-xml"
        r = ctx["session"].get(url, timeout=10)
        data = r.json().get("js", {})
        return {
            "mac": data.get("mac", ""),
            "phone": data.get("phone", ""),
            "status": data.get("status", ""),
            "max_connections": data.get("max_connections", ""),
        }
    except:
        return {}


# ─── Adult unlock ─────────────────────────────────────────────────────────────

def unlock_adult(ctx):
    sess = ctx["session"]
    base = ctx["base_url"]
    pt   = ctx["portal_type"]
    for pin in ["0000", "1234", "3333", "4444", "9999"]:
        try:
            r  = sess.get(f"{base}{pt}?type=itv&action=set_parental_lock&password={pin}&JsHttpRequest=1-xml", timeout=8)
            js = r.json().get("js", {})
            if js is True or (isinstance(js, dict) and js.get("result") in (True, "true", 1)):
                return True
        except: continue
    return False


# ─── Category fetchers (lightweight — no channels) ────────────────────────────

def fetch_live_genres(ctx):
    """Returns list of {id, name, censored} for live TV genres.
    Tries portal_type first, then fairy-root's server/load.php fallback."""
    try:
        url = f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_genres&JsHttpRequest=1-xml"
        r   = ctx["session"].get(url, timeout=15)
        gs  = r.json().get("js", [])
        if isinstance(gs, list) and gs:
            out = []
            for g in gs:
                gid = str(g.get("id",""))
                if gid in ("*","pvr","dvb",""): continue
                out.append({
                    "id":       gid,
                    "name":     g.get("title",""),
                    "censored": bool(g.get("censored",0)),
                    "type":     "live",
                })
            if out:
                return out
    except: pass

    try:
        url = f"{ctx['base_url']}/server/load.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
        r   = ctx["session"].get(url, timeout=15)
        gs  = r.json().get("js", [])
        if isinstance(gs, list):
            return [{
                "id":       str(g.get("id","")),
                "name":     g.get("title",""),
                "censored": False,
                "type":     "live",
            } for g in gs if str(g.get("id","")) not in ("*","pvr","dvb","")]
    except: pass
    return []


def fetch_vod_categories(ctx, content_type="vod"):
    ctype = "series" if content_type == "series" else "vod"
    try:
        url  = f"{ctx['base_url']}{ctx['portal_type']}?type={ctype}&action=get_categories&JsHttpRequest=1-xml"
        r    = ctx["session"].get(url, timeout=15)
        cats = r.json().get("js", [])
        if not isinstance(cats, list): return []
        return [{
            "id":       str(c.get("id","")),
            "name":     c.get("title",""),
            "censored": False,
            "type":     content_type,
        } for c in cats if str(c.get("id",""))]
    except: return []


# ─── NEW: Fast live fetch (fairy-root maclist.py) ─────────────────────────────

def fetch_all_live_channels(ctx):
    """Try to get ALL live channels in a single API call.
    Returns list of channel dicts, or None if portal doesn't support it."""
    for path in [f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_all_channels&JsHttpRequest=1-xml",
                 f"{ctx['base_url']}/portal.php?type=itv&action=get_all_channels&JsHttpRequest=1-xml"]:
        try:
            r = ctx["session"].get(path, timeout=30)
            r.raise_for_status()
            data = r.json().get("js", {}).get("data", [])
            if not data:
                continue

            genres = fetch_live_genres(ctx)
            genre_map = {g["id"]: g["name"] for g in genres}
            if not genre_map:
                try:
                    url_g = f"{ctx['base_url']}/server/load.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
                    r_g = ctx["session"].get(url_g, timeout=15)
                    gs = r_g.json().get("js", [])
                    genre_map = {str(g.get("id","")): g.get("title","") for g in gs if str(g.get("id","")) not in ("*","pvr","dvb","")}
                except: pass

            out = []
            for ch in data:
                gid = str(ch.get("tv_genre_id", "0"))
                cmd_raw = ch.get("cmds", [{}])[0].get("url", "")
                cmd = cmd_raw[7:] if cmd_raw.startswith("ffmpeg ") else cmd_raw

                if "localhost" in cmd and "/ch/" in cmd:
                    m = re.search(r"/ch/(\d+)", cmd)
                    if m:
                        ch_id = m.group(1)
                        cmd = f"{ctx['base_url']}/play/live.php?mac={ctx['mac']}&stream={ch_id}&extension=ts"

                if not cmd:
                    continue

                out.append({
                    "id": str(ch.get("id","")),
                    "name": ch.get("name","Unknown"),
                    "number": ch.get("number",""),
                    "logo": ch.get("logo",""),
                    "cmd": cmd,
                    "genre": genre_map.get(gid, "General"),
                    "genre_id": gid,
                    "content_type": "live",
                })
            return out
        except Exception:
            continue
    return None


# ─── Channel fetchers (per selected category, streamed) ───────────────────────

def iter_live_genre(ctx, genre_id, genre_name, is_adult=False):
    """Yield channels or metadata for one live genre, paginated."""
    page           = 1
    censored_param = "&censored=1" if is_adult else ""
    while True:
        url = (f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_ordered_list"
               f"&genre={genre_id}&force_ch_link_check=&fav=0&sortby=number&hd=0"
               f"&p={page}{censored_param}&JsHttpRequest=1-xml")
        try:
            r        = ctx["session"].get(url, timeout=20)
            js       = r.json().get("js", {})
            data     = js.get("data", [])
            total    = int(js.get("total_items", 0))
            if page == 1:
                yield {"_cat_total_channels": total}
            for ch in data:
                yield {
                    "id": str(ch.get("id","")), "name": ch.get("name","Unknown"),
                    "number": ch.get("number",""), "logo": ch.get("logo",""),
                    "cmd": ch.get("cmd",""), "genre": genre_name,
                    "genre_id": genre_id, "content_type": "live",
                }
            per_page = int(js.get("max_page_items", len(data) or 1))
            pages    = math.ceil(total / per_page) if per_page else 1
            if page >= pages or not data: break
            page += 1
        except: break


# ─── Streaming live fetcher (VOD-style parallel pages) ────────────────────────
# Mirrors iter_vod_category: page 1 first, pages 2..N fetched concurrently via
# ThreadPool, each yielded as its future completes. Gives the same "fast
# increase" progress behaviour as VOD instead of strictly sequential pages.

def iter_live_category(ctx, genre_id, genre_name, is_adult=False):
    """Generator: yields live channels for one genre as each page completes.
    First yields {'_total': total_items}, then each channel dict as it arrives."""
    censored_param = "&censored=1" if is_adult else ""
    url = (f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_ordered_list"
           f"&genre={genre_id}&force_ch_link_check=&fav=0&sortby=number&hd=0"
           f"&p=1{censored_param}&JsHttpRequest=1-xml")
    try:
        r        = ctx["session"].get(url, timeout=30)
        js       = r.json().get("js", {})
        data     = js.get("data", [])
        total    = int(js.get("total_items", 0))
        per_page = int(js.get("max_page_items", len(data) or 1))
        pages    = math.ceil(total / per_page) if per_page else 1
    except Exception:
        return

    def parse_channels(page_data):
        out = []
        for ch in page_data:
            out.append({
                "id": str(ch.get("id","")), "name": ch.get("name","Unknown"),
                "number": ch.get("number",""), "logo": ch.get("logo",""),
                "cmd": ch.get("cmd",""), "genre": genre_name,
                "genre_id": genre_id, "content_type": "live",
            })
        return out

    yield {"_total": total}

    for ch in parse_channels(data):
        yield ch

    if pages > 1:
        def fetch_page(p):
            purl = (f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_ordered_list"
                    f"&genre={genre_id}&force_ch_link_check=&fav=0&sortby=number&hd=0"
                    f"&p={p}{censored_param}&JsHttpRequest=1-xml")
            for attempt in range(3):
                try:
                    pr = ctx["session"].get(purl, timeout=30)
                    data = pr.json().get("js", {}).get("data", [])
                    if data or attempt == 2:
                        return data
                except:
                    pass
                if attempt < 2:
                    time.sleep(1)
            return []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_page, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                for ch in parse_channels(future.result()):
                    yield ch


# ─── NEW: Parallel VOD page fetcher (fairy-root style) ─────────────────────────

def fetch_vod_category_parallel(ctx, cat_id, cat_name, content_type="vod"):
    """Fetch all pages for a VOD/series category using ThreadPool (up to 10 workers).
    Returns (total_items, list of item dicts)."""
    ctype = "series" if content_type == "series" else "vod"
    url = (f"{ctx['base_url']}{ctx['portal_type']}?type={ctype}&action=get_ordered_list"
           f"&category={cat_id}&fav=0&sortby=added&hd=0&p=1&JsHttpRequest=1-xml")
    try:
        r = ctx["session"].get(url, timeout=30)
        js = r.json().get("js", {})
        data = js.get("data", [])
        total = int(js.get("total_items", 0))
        per_page = int(js.get("max_page_items", len(data) or 1))
        pages = math.ceil(total / per_page) if per_page else 1
    except Exception:
        return 0, []

    all_items = []
    def parse_items(page_data):
        items = []
        for item in page_data:
            iid = str(item.get("id",""))
            cmd = (item.get("cmd","")
                or item.get("series_cmd","")
                or item.get("url","")
                or item.get("link",""))
            items.append({
                "id":           iid,
                "name":         item.get("name", item.get("title","Unknown")),
                "number":       item.get("number",""),
                "logo":         item.get("screenshot_url", item.get("screenshot_uri", item.get("logo",""))),
                "cmd":          cmd,
                "series_id":    iid,
                "genre":        cat_name,
                "genre_id":     cat_id,
                "content_type": content_type,
            })
        return items

    all_items.extend(parse_items(data))

    if pages > 1:
        def fetch_page(p):
            purl = (f"{ctx['base_url']}{ctx['portal_type']}?type={ctype}&action=get_ordered_list"
                    f"&category={cat_id}&fav=0&sortby=added&hd=0&p={p}&JsHttpRequest=1-xml")
            for attempt in range(3):
                try:
                    pr = ctx["session"].get(purl, timeout=30)
                    data = pr.json().get("js", {}).get("data", [])
                    if data or attempt == 2:
                        return data
                except:
                    pass
                if attempt < 2:
                    time.sleep(1)
            return []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_page, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                all_items.extend(parse_items(future.result()))

    return total, all_items


# ─── NEW: Streaming VOD/series fetcher — yields items as pages complete ───────
# Mirrors fetch_vod_category_parallel's parsing & parallelism, but yields each
# page's items the moment its future resolves (as_completed) instead of
# buffering the whole category and flushing at the end. This spreads item
# events across the real fetch duration so progress advances smoothly instead
# of one big jump — same behaviour iter_live_genre gives for live TV.

def iter_vod_category(ctx, cat_id, cat_name, content_type="vod"):
    """Generator: yields items of a VOD/series category as each page completes.
    First yields a sentinel {'_total': total_items} so callers can report the
    category total up front, then yields each parsed item dict as it arrives."""
    ctype = "series" if content_type == "series" else "vod"
    url = (f"{ctx['base_url']}{ctx['portal_type']}?type={ctype}&action=get_ordered_list"
           f"&category={cat_id}&fav=0&sortby=added&hd=0&p=1&JsHttpRequest=1-xml")
    try:
        r = ctx["session"].get(url, timeout=30)
        js = r.json().get("js", {})
        data = js.get("data", [])
        total = int(js.get("total_items", 0))
        per_page = int(js.get("max_page_items", len(data) or 1))
        pages = math.ceil(total / per_page) if per_page else 1
    except Exception:
        return  # empty generator

    def parse_items(page_data):
        items = []
        for item in page_data:
            iid = str(item.get("id",""))
            cmd = (item.get("cmd","")
                or item.get("series_cmd","")
                or item.get("url","")
                or item.get("link",""))
            items.append({
                "id":           iid,
                "name":         item.get("name", item.get("title","Unknown")),
                "number":       item.get("number",""),
                "logo":         item.get("screenshot_url", item.get("screenshot_uri", item.get("logo",""))),
                "cmd":          cmd,
                "series_id":    iid,
                "genre":        cat_name,
                "genre_id":     cat_id,
                "content_type": content_type,
            })
        return items

    # Report the category total first.
    yield {"_total": total}

    # Page 1 — yield immediately (already fetched).
    for item in parse_items(data):
        yield item

    # Pages 2..N — fetched concurrently; yield each page as it completes so
    # progress advances over the fetch duration instead of dumping at the end.
    if pages > 1:
        def fetch_page(p):
            purl = (f"{ctx['base_url']}{ctx['portal_type']}?type={ctype}&action=get_ordered_list"
                    f"&category={cat_id}&fav=0&sortby=added&hd=0&p={p}&JsHttpRequest=1-xml")
            for attempt in range(3):
                try:
                    pr = ctx["session"].get(purl, timeout=30)
                    data = pr.json().get("js", {}).get("data", [])
                    if data or attempt == 2:
                        return data
                except:
                    pass
                if attempt < 2:
                    time.sleep(1)
            return []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_page, p) for p in range(2, pages + 1)]
            for future in as_completed(futures):
                for item in parse_items(future.result()):
                    yield item


# ─── NEW: Full series episode fetcher (fairy-root macshow.py) ─────────────────

def get_series_all_episodes(ctx, series_id, category_id):
    """Fetch all seasons and episodes for a series.
    Returns list of {season_num, episode_num, total_episodes}."""
    try:
        url = (f"{ctx['base_url']}{ctx['portal_type']}?type=series&action=get_ordered_list"
               f"&movie_id={quote(series_id)}&season_id=0&episode_id=0&row=0&JsHttpRequest=1-xml"
               f"&category={category_id}&sortby=added&fav=0&hd=0&not_ended=0&abc=*&genre=*&years=*&search=&p=1")
        r = ctx["session"].get(url, timeout=15)
        data = r.json().get("js", {}).get("data", [])
        episodes = []
        for season in data:
            sid = str(season.get("id", ""))
            season_num = sid.split(":")[1] if ":" in sid else sid
            ep_list = season.get("series", [])
            total_eps = len(ep_list)
            for ep_num in ep_list:
                episodes.append({
                    "season_num": int(season_num) if str(season_num).isdigit() else 0,
                    "episode_num": ep_num,
                    "total_episodes": total_eps,
                })
        return episodes
    except:
        return []


# ─── NEW: Base64 series cmd builder (fairy-root) ─────────────────────────────

def build_series_cmd(series_id, season_num):
    cmd_data = {"series_id": series_id, "season_num": int(season_num), "type": "series"}
    return base64.b64encode(json.dumps(cmd_data).encode()).decode()


# ─── Stream URL resolver ──────────────────────────────────────────────────────

def resolve_stream_url(ctx, cmd, content_type="live", series_id="", season_num=0, episode_num=0):
    """
    Resolve a cmd to a real playable stream URL.
    Merged with fairy-root logic:
    - live:  localhost fix, create_link for /ch/ proxy
    - vod:   create_link, split on space for ffmpeg prefix
    - series: base64 cmd + create_link with episode number
    """
    try:
        raw = cmd[7:] if cmd.startswith("ffmpeg ") else cmd

        if content_type == "live":
            if "/ch/" in raw and raw.endswith("_"):
                enc    = quote(cmd)
                url    = (f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=create_link"
                          f"&cmd={enc}&series=&forced_storage=undefined&disable_ad=0&download=0&JsHttpRequest=1-xml")
                r      = ctx["session"].get(url, timeout=10)
                result = r.json().get("js",{}).get("cmd","")
                if result:
                    return result[7:] if result.startswith("ffmpeg ") else result
            if "localhost" in raw and "/ch/" in raw:
                m = re.search(r"/ch/(\d+)", raw)
                if m:
                    ch_id = m.group(1)
                    return f"{ctx['base_url']}/play/live.php?mac={ctx['mac']}&stream={ch_id}&extension=ts"
            return raw

        elif content_type == "series":
            if not raw and series_id:
                raw = build_series_cmd(series_id, season_num or 1)
            if not raw:
                return ""
            enc    = quote(raw)
            url    = (f"{ctx['base_url']}{ctx['portal_type']}?type=vod&action=create_link"
                      f"&cmd={enc}&series={episode_num or 1}&forced_storage=undefined&disable_ad=0&download=0&JsHttpRequest=1-xml")
            r      = ctx["session"].get(url, timeout=10)
            result = r.json().get("js",{}).get("cmd","")
            if result:
                if ' ' in result:
                    return result.split(' ')[1]
                return result[7:] if result.startswith("ffmpeg ") else result
            return raw

        else:  # vod
            if not raw:
                return ""
            enc    = quote(cmd)
            url    = (f"{ctx['base_url']}{ctx['portal_type']}?type=vod&action=create_link"
                      f"&cmd={enc}&series=&forced_storage=undefined&disable_ad=0&download=0&JsHttpRequest=1-xml")
            r      = ctx["session"].get(url, timeout=10)
            result = r.json().get("js",{}).get("cmd","")
            if result:
                if ' ' in result:
                    return result.split(' ')[1]
                return result[7:] if result.startswith("ffmpeg ") else result
            return raw

    except:
        return cmd[7:] if cmd.startswith("ffmpeg ") else cmd


def extract_expiry(profile, main_info):
    combined = {}
    if profile:
        combined.update(profile)
    if main_info:
        combined.update(main_info)
    if not combined:
        return ""
    candidates = [
        "phone", "end_date", "expire_billing_date", "exp_date",
        "expiration", "tariff_expired_date", "tariff_expiration",
        "account_expiration", "subscription_end", "valid_until",
        "exp", "expiry", "expiration_date", "subscription_expiry",
        "billing_date", "renewal_date",
    ]
    invalid_values = ["", "0", "null", "none", "n/a", "0000-00-00 00:00:00", "0000-00-00"]
    for key in candidates:
        val = combined.get(key)
        if val is not None:
            str_val = str(val).strip()
            if str_val.lower() not in invalid_values and str_val:
                return str_val
    return ""


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("static/index.html")


@app.route("/api/check", methods=["POST"])
def check():
    data       = request.json
    portal_url = data.get("portal_url","").strip()
    mac        = data.get("mac","").strip().upper()

    if not portal_url: return jsonify({"ok":False,"error":"Portal URL is required"}),400
    if not mac:        return jsonify({"ok":False,"error":"MAC address is required"}),400
    if not re.match(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$', mac):
        return jsonify({"ok":False,"error":"Invalid MAC format (e.g. 00:1A:79:XX:XX:XX)"}),400

    ctx = get_token(portal_url, mac)
    if not ctx:
        return jsonify({"ok":False,"error":"Authentication failed — check portal URL and MAC"}),400

    profile  = auth_session(ctx)
    main_info = get_main_info(ctx)
    expiry   = extract_expiry(profile, main_info)
    status   = main_info.get("status") or (profile.get("status","active") if profile else "active") or "active"
    max_conn = main_info.get("max_connections") or (profile.get("max_connections","") if profile else "")

    return jsonify({
        "ok": True,
        "sn": ctx["sn"], "device_id": ctx["device_id"],
        "device_id2": ctx["device_id2"],
        "expiry": expiry, "status": status, "max_connections": max_conn,
    })


@app.route("/api/categories", methods=["POST"])
def categories():
    data          = request.json
    portal_url    = data.get("portal_url","").strip()
    mac           = data.get("mac","").strip().upper()
    content_types = data.get("content_types", ["live","vod","series"])

    ctx = get_token(portal_url, mac)
    if not ctx:
        return jsonify({"ok":False,"error":"Authentication failed"}),400
    auth_session(ctx)

    if "live" in content_types:
        unlock_adult(ctx)

    result = {}
    if "live"   in content_types: result["live"]   = fetch_live_genres(ctx)
    if "vod"    in content_types: result["vod"]    = fetch_vod_categories(ctx,"vod")
    if "series" in content_types: result["series"] = fetch_vod_categories(ctx,"series")
    return jsonify({"ok": True, "categories": result})


@app.route("/api/category-count", methods=["POST"])
def category_count():
    data = request.json
    portal_url = data.get("portal_url","").strip()
    mac = data.get("mac","").strip().upper()
    ct = data.get("type", "live")
    cid = data.get("category_id", "")
    if not portal_url or not mac or not cid:
        return jsonify({"ok":False,"error":"Missing params"}),400
    ctx = get_token(portal_url, mac)
    if not ctx:
        return jsonify({"ok":False,"error":"Auth failed"}),400
    auth_session(ctx)
    try:
        if ct == "live":
            url = f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_ordered_list&genre={cid}&force_ch_link_check=&fav=0&sortby=number&hd=0&p=1&JsHttpRequest=1-xml"
        else:
            pt = "series" if ct == "series" else "vod"
            url = f"{ctx['base_url']}{ctx['portal_type']}?type={pt}&action=get_ordered_list&category={cid}&fav=0&sortby=added&hd=0&p=1&JsHttpRequest=1-xml"
        r = ctx["session"].get(url, timeout=15)
        js = r.json().get("js", {})
        return jsonify({"ok":True,"count":int(js.get("total_items", 0))})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500


@app.route("/api/enrich-counts", methods=["POST"])
def enrich_counts():
    data = request.json
    portal_url = data.get("portal_url","").strip()
    mac = data.get("mac","").strip().upper()
    cats = data.get("cats", [])
    if not portal_url or not mac or not cats:
        return jsonify({"ok":False,"error":"Missing params"}),400
    ctx = get_token(portal_url, mac)
    if not ctx:
        return jsonify({"ok":False,"error":"Auth failed"}),400
    auth_session(ctx)

    def get_count(cat):
        ct = cat.get("type", "live")
        cid = cat["id"]
        for attempt in range(3):
            try:
                if ct == "live":
                    url = f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_ordered_list&genre={cid}&force_ch_link_check=&fav=0&sortby=number&hd=0&p=1&JsHttpRequest=1-xml"
                else:
                    pt = "series" if ct == "series" else "vod"
                    url = f"{ctx['base_url']}{ctx['portal_type']}?type={pt}&action=get_ordered_list&category={cid}&fav=0&sortby=added&hd=0&p=1&JsHttpRequest=1-xml"
                r = ctx["session"].get(url, timeout=30)
                js = r.json().get("js", {})
                count = int(js.get("total_items", 0))
                if count > 0 or attempt == 2:
                    return {"type": ct, "id": cid, "count": count}
            except:
                pass
            if attempt < 2:
                time.sleep(1)
        return {"type": ct, "id": cid, "count": 0}

    with ThreadPoolExecutor(max_workers=10) as ex:
        counts = list(ex.map(get_count, cats))
    return jsonify({"ok":True,"counts":counts})


@app.route("/api/fetch-category", methods=["POST"])
def fetch_category():
    data = request.json
    portal_url = (data.get("portal_url","") or "").strip()
    mac = (data.get("mac","") or "").strip().upper()
    cat_type = data.get("type")
    cat_id = data.get("category_id","")
    cat_name = data.get("category_name","")
    filename = data.get("filename","")
    if not all([portal_url, mac, cat_type, cat_id, filename]):
        return jsonify({"ok":False,"error":"Missing params"}),400
    ctx = get_token(portal_url, mac)
    if not ctx: return jsonify({"ok":False,"error":"Auth failed"}),400
    auth_session(ctx)
    if cat_type == "live": unlock_adult(ctx)

    items = []
    portal_total = None
    if cat_type == "live":
        for item in iter_live_category(ctx, cat_id, cat_name):
            if isinstance(item, dict) and "_total" in item:
                portal_total = item["_total"]
                continue
            item["content_type"] = "live"
            if item.get("cmd"): item["needsResolve"] = True
            item["portal_url"] = portal_url
            item["mac"] = mac
            items.append(item)
    else:
        ctype = cat_type
        for item in iter_vod_category(ctx, cat_id, cat_name, content_type=ctype):
            if isinstance(item, dict) and "_total" in item:
                portal_total = item["_total"]
                continue
            item["content_type"] = ctype
            if item.get("cmd"): item["needsResolve"] = True
            item["portal_url"] = portal_url
            item["mac"] = mac
            items.append(item)

    # Strip unnecessary fields before saving
    for item in items:
        item.pop("number", None)
        item.pop("logo", None)
        item.pop("genre", None)
        item.pop("group", None)
        item.pop("url", None)

    fp = os.path.join(PLAYLIST_DIR, filename + ".json")
    if os.path.isfile(fp):
        with open(fp, "r") as f:
            jdata = json.load(f)
    else:
        jdata = {"categories": {}}
    # Transition from old flat format
    jdata.pop("channels", None)
    jdata.setdefault("categories", {})
    for ct in ("live","vod","series"):
        jdata["categories"].setdefault(ct, [])

    added = 0
    if cat_type in jdata["categories"]:
        for cat in jdata["categories"][cat_type]:
            if str(cat.get("id")) == str(cat_id):
                cat["cached"] = True
                if cat_type == "series":
                    # Merge series entries preserving existing episode data
                    existing = {s.get("id"): s for s in cat.get("series", [])}
                    for it in items:
                        sid = it.get("series_id", it.get("id", ""))
                        if not sid:
                            continue
                        if sid in existing:
                            existing[sid].setdefault("episode", [])
                        else:
                            existing[sid] = {"id": sid, "name": it.get("name", ""), "episode": []}
                    cat["series"] = list(existing.values())
                elif cat_type == "live":
                    cat["Channel"] = items
                elif cat_type == "vod":
                    cat["Movie"] = items
                added = len(items)
                break

    jdata["updated"] = time.time()
    os.makedirs(PLAYLIST_DIR, exist_ok=True)
    with open(fp, "w") as f:
        json.dump(jdata, f, indent=2)

    return jsonify({"ok":True,"total":len(items),"portal_total":portal_total,"added":added,"items":items})


@app.route("/api/fetch-series-episodes", methods=["POST"])
def fetch_series_episodes():
    data = request.json
    portal_url = (data.get("portal_url","") or "").strip()
    mac = (data.get("mac","") or "").strip().upper()
    series_id = data.get("series_id","")
    series_name = data.get("series_name","")
    category_name = data.get("category_name","")
    filename = data.get("filename","")
    if not all([portal_url, mac, series_id, filename]):
        return jsonify({"ok":False,"error":"Missing params"}),400
    ctx = get_token(portal_url, mac)
    if not ctx: return jsonify({"ok":False,"error":"Auth failed"}),400
    auth_session(ctx)

    category_id = None
    episodes = get_series_all_episodes(ctx, series_id, category_id)
    items = []
    for ep in episodes:
        sn = int(ep.get("season_num", 1))
        en = int(ep.get("episode_num", 1))
        cmd = build_series_cmd(series_id, sn)
        items.append({
            "name": f"{series_name} S{sn:02d} E{en:02d}",
            "series_id": series_id,
            "season_num": sn,
            "episode_num": en,
            "cmd": cmd,
            "url": cmd,
            "group": category_name,
            "content_type": "series",
            "needsResolve": True,
            "portal_url": portal_url,
            "mac": mac,
        })

    for item in items:
        item.pop("url", None)

    fp = os.path.join(PLAYLIST_DIR, filename + ".json")
    if os.path.isfile(fp):
        with open(fp, "r") as f:
            jdata = json.load(f)
    else:
        jdata = {"categories": {"series": []}}
    # Transition from old flat format
    jdata.pop("channels", None)
    jdata.setdefault("categories", {})
    jdata["categories"].setdefault("series", [])

    # Build minimal episodes for storage
    new_eps = [{"season_num": e["season_num"], "episode_num": e["episode_num"], "cmd": e["cmd"]} for e in items]

    channels_added = []
    if category_name:
        for cat in jdata["categories"]["series"]:
            if cat.get("name") == category_name:
                for s_entry in cat.setdefault("series", []):
                    if str(s_entry.get("id")) == str(series_id):
                        existing = {}
                        for e in s_entry.get("episode", []):
                            existing[(e["season_num"], e["episode_num"])] = e
                        for e in new_eps:
                            key = (e["season_num"], e["episode_num"])
                            if key not in existing:
                                existing[key] = e
                                channels_added.append(e)
                        s_entry["episode"] = list(existing.values())
                        break
                break

    jdata["updated"] = time.time()
    os.makedirs(PLAYLIST_DIR, exist_ok=True)
    with open(fp, "w") as f:
        json.dump(jdata, f, indent=2)

    return jsonify({"ok":True,"total":len(items),"added":len(channels_added),"items":items})


@app.route("/api/recover-categories", methods=["POST"])
def recover_categories():
    """Sequential retry of category pages that may have failed on first pass."""
    data = request.json
    portal_url = (data.get("portal_url","") or "").strip()
    mac = (data.get("mac","") or "").strip().upper()
    categories = data.get("categories", [])
    if not portal_url or not mac or not categories:
        return jsonify({"ok":False,"error":"Missing params"}),400
    ctx = get_token(portal_url, mac)
    if not ctx: return jsonify({"ok":False,"error":"Auth failed"}),400
    auth_session(ctx)
    results = []

    for cat_def in categories:
        cat_type = cat_def.get("type", "live")
        cat_id = cat_def.get("category_id", "")
        cat_name = cat_def.get("category_name", "")
        filename = cat_def.get("filename", "")
        portal_total = cat_def.get("portal_total", 0)
        if not cat_id or not filename:
            results.append({"category_name":cat_name,"error":"Missing category_id or filename","ok":False})
            continue

        # Step 1: fetch page 1 to get max_page_items
        try:
            if cat_type == "live":
                p1_url = f"{ctx['base_url']}{ctx['portal_type']}?type=itv&action=get_ordered_list&genre={cat_id}&force_ch_link_check=&fav=0&sortby=number&hd=0&p=1&JsHttpRequest=1-xml"
                ctype_param = "genre"
                ctype_val = cat_id
                content_type_label = "live"
            else:
                pt = "series" if cat_type == "series" else "vod"
                p1_url = f"{ctx['base_url']}{ctx['portal_type']}?type={pt}&action=get_ordered_list&category={cat_id}&fav=0&sortby=added&hd=0&p=1&JsHttpRequest=1-xml"
                ctype_param = "category"
                ctype_val = cat_id
                content_type_label = cat_type

            for attempt in range(3):
                try:
                    r = ctx["session"].get(p1_url, timeout=30)
                    js = r.json().get("js", {})
                    p1_data = js.get("data", [])
                    actual_total = int(js.get("total_items", 0))
                    max_page_items = int(js.get("max_page_items", len(p1_data) or 1))
                    if max_page_items > 0:
                        break
                except:
                    pass
                if attempt < 2: time.sleep(1)
            else:
                results.append({"category_name":cat_name,"error":"Failed to fetch page 1","ok":False})
                continue
        except:
            results.append({"category_name":cat_name,"error":"Page 1 error","ok":False})
            continue

        expected_total = max(portal_total, actual_total)
        pages = math.ceil(expected_total / max_page_items) if max_page_items else 1
        if pages <= 1:
            results.append({"category_name":cat_name,"ok":True,"had_items":len(p1_data),"portal_total":expected_total,"recovered":0,"missing":0,"complete":True})
            continue

        # Build page 1 items
        def parse_item(raw):
            iid = str(raw.get("id",""))
            if cat_type == "live":
                return {"id":iid,"name":raw.get("name","Unknown"),"cmd":raw.get("cmd","")}
            else:
                cmd = raw.get("cmd","") or raw.get("series_cmd","") or raw.get("url","") or raw.get("link","")
                return {"id":iid,"name":raw.get("name",raw.get("title","Unknown")),"cmd":cmd}

        all_items = [parse_item(x) for x in p1_data]
        recovered_count = 0
        failed_recovery = []

        # Sequential retry of pages 2..N
        for p in range(2, pages + 1):
            p_url = (f"{ctx['base_url']}{ctx['portal_type']}?type={'itv' if cat_type=='live' else ('series' if cat_type=='series' else 'vod')}&action=get_ordered_list&{ctype_param}={ctype_val}&fav=0&sortby={'number' if cat_type=='live' else 'added'}&hd=0&p={p}&JsHttpRequest=1-xml")
            page_data = None
            for attempt in range(3):
                try:
                    pr = ctx["session"].get(p_url, timeout=30)
                    pd = pr.json().get("js", {}).get("data", [])
                    if pd or attempt == 2:
                        page_data = pd
                        break
                except:
                    pass
                if attempt < 2: time.sleep(1)
            if page_data:
                new_items = [parse_item(x) for x in page_data]
                # Dedup by id
                existing_ids = {it["id"] for it in all_items}
                for ni in new_items:
                    if ni["id"] not in existing_ids:
                        all_items.append(ni)
                        existing_ids.add(ni["id"])
                        recovered_count += 1
            else:
                failed_recovery.append(p)

        # Save to JSON
        fp = os.path.join(PLAYLIST_DIR, filename + ".json")
        if os.path.isfile(fp):
            with open(fp, "r") as f:
                jdata = json.load(f)
        else:
            jdata = {"categories": {}}
        jdata.pop("channels", None)
        jdata.setdefault("categories", {})
        for ct in ("live","vod","series"):
            jdata["categories"].setdefault(ct, [])

        if cat_type in jdata["categories"]:
            for cat in jdata["categories"][cat_type]:
                if str(cat.get("id")) == str(cat_id):
                    cat["cached"] = True
                    if cat_type == "series":
                        existing = {str(s.get("id")): s for s in cat.get("series", [])}
                        for it in all_items:
                            sid = it.get("id", "")
                            if not sid: continue
                            key = str(sid)
                            if key in existing:
                                existing[key].setdefault("episode", [])
                            else:
                                existing[key] = {"id": key, "name": it.get("name", ""), "episode": []}
                        cat["series"] = list(existing.values())
                        cat["count"] = len(all_items)
                    elif cat_type == "live":
                        cat["Channel"] = all_items
                        cat["count"] = len(all_items)
                    elif cat_type == "vod":
                        cat["Movie"] = all_items
                        cat["count"] = len(all_items)
                    break

        jdata["updated"] = time.time()
        with open(fp, "w") as f:
            json.dump(jdata, f, indent=2)

        missing = expected_total - len(all_items)
        results.append({
            "category_name":cat_name,
            "ok":True,
            "had_items":len(p1_data),
            "portal_total":expected_total,
            "total":len(all_items),
            "recovered":recovered_count,
            "missing":max(0, missing),
            "failed_pages":failed_recovery,
            "complete": missing <= 0 and not failed_recovery
        })

    return jsonify({"ok":True,"results":results})


# ─── Convert control state (pause/stop per convert_id) ────────────────────────
# Each in-flight convert registers {state, event}. 'state' is one of:
#   running | paused | stopped
# The generator checks this after every entry under its lock.
_CONVERT_CTRL = {}
_CONVERT_LOCK = threading.Lock()

def _ctrl_new(cid):
    with _CONVERT_LOCK:
        _CONVERT_CTRL[cid] = {"state": "running", "event": threading.Event()}

def _ctrl_get(cid):
    with _CONVERT_LOCK:
        c = _CONVERT_CTRL.get(cid)
        return c["state"] if c else None



@app.route("/api/convert", methods=["POST"])
def convert():
    """SSE stream: build the M3U with live [done/total] progress, parallel
    resolves (preserving original order), and pause/stop support, or save
    as JSON playlist.

    Events (M3U mode):
      {_start, convert_id, total}            — first event, carries the id for control
      {_lines, lines:[...], done, total}     — resolved EXTINF+URL pair, streamed
      {_paused} / {_resumed}                 — state changes (optional)
      {_done, total, file}                   — finished, file = full M3U text
      {_stopped, total, file}                — stopped, file = partial M3U text

    resolve=False: instant build, single {_done} with file (no progress/pause).

    Events (JSON mode):
      {_start, total, filename}              — started
      {_lines, lines:[...], done, total}     — each channel entry
      {_done, filename}                      — finished
    """
    data       = request.json
    portal_url = data.get("portal_url","").strip()
    mac        = data.get("mac","").strip().upper()
    channels   = data.get("channels",[])
    fmt        = data.get("format", "m3u")

    if not portal_url or not mac or not channels:
        return jsonify({"ok":False,"error":"Missing required fields"}),400

    if fmt == "json":
        return _convert_to_json(portal_url, mac, channels)

    resolve    = data.get("resolve_urls", True)
    ctx = get_token(portal_url, mac)
    if not ctx:
        return jsonify({"ok":False,"error":"Re-authentication failed"}),400
    auth_session(ctx)

    # Build the list of (slot, work_fn) units. Each unit produces the EXTINF+URL
    # lines for one channel (or all its episodes). Slots preserve original order.
    units = []
    for idx, ch in enumerate(channels):
        name  = ch.get("name","Unknown")
        num   = ch.get("number","0")
        logo  = ch.get("logo","")
        cmd   = ch.get("cmd","")
        genre = ch.get("genre","IPTV")
        ctype = ch.get("content_type","live")
        sid   = ch.get("series_id","") or ch.get("id","")
        ep_count = len(ch["episodes"]) if (ctype == "series" and ch.get("episodes")) else 0

        def make_unit(idx, name, num, logo, cmd, genre, ctype, sid, ep_count):
            def work(ctx_local=ctx):
                out = []
                if ctype == "series" and ep_count and resolve:
                    eps = ch["episodes"]
                    for ep in eps:
                        snum = ep["season_num"]
                        enum = ep["episode_num"]
                        ep_total = ep["total_episodes"]
                        pad = len(str(ep_total))
                        ep_title = f"{name} S{snum} E{enum:0{pad}d}"
                        cmd_b64 = build_series_cmd(sid, snum)
                        stream_url = resolve_stream_url(ctx_local, cmd_b64, content_type="series",
                                                        series_id=sid, season_num=snum, episode_num=enum)
                        if not stream_url:
                            stream_url = f"# no stream found for: {ep_title}"
                        out.append(
                            f'#EXTINF:-1 tvg-type="serie" tvg-serie="{sid}" tvg-season="{snum}" '
                            f'tvg-episode="{enum}" serie-title="{name}" tvg-logo="{logo}" '
                            f'group-title="{genre}",{ep_title}'
                        )
                        out.append(stream_url)
                else:
                    if resolve:
                        stream_url = resolve_stream_url(ctx_local, cmd, content_type=ctype, series_id=sid)
                    else:
                        stream_url = cmd[7:] if cmd.startswith("ffmpeg ") else cmd
                    if not stream_url:
                        stream_url = f"# no stream found for: {name}"
                    out.append(f'#EXTINF:-1 tvg-id="{num}" tvg-name="{name}" tvg-logo="{logo}" group-title="{genre}",{name}')
                    out.append(stream_url)
                return idx, out
            return work

        units.append((idx, ep_count or 1, make_unit(idx, name, num, logo, cmd, genre, ctype, sid, ep_count)))

    # Progress denominator: episode count for series, else 1 per channel.
    total = sum(weight for _, weight, _ in units) if resolve else 0

    def sse(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    # ── Instant path: no resolving, no progress/pause ──
    if not resolve:
        lines = ["#EXTM3U"]
        for _, _, work in units:
            _, out = work()
            lines.extend(out)
        return Response(
            sse({"_done": True, "total": len(channels), "file": "\n".join(lines) + "\n"}),
            mimetype="text/event-stream",
            headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"},
        )

    # ── Streaming path: parallel resolves, preserve order, pause/stop ──
    convert_id = uuid.uuid4().hex
    _ctrl_new(convert_id)

    def generate():
        try:
            yield sse({"_start": True, "convert_id": convert_id, "total": total})

            results = {}   # slot -> list[str] (assembled in original order at the end)
            done    = 0
            stopped = False
            last_state = "running"

            def check_control():
                """Return one of: 'running' | 'stopped'. Blocks while paused."""
                nonlocal last_state
                while True:
                    st = _ctrl_get(convert_id)
                    if st is None or st == "running":
                        if last_state == "paused":
                            last_state = "running"
                            return ("resumed", "running")
                        return (None, "running")
                    if st == "stopped":
                        return ("stopped", "stopped")
                    # paused → wait until something changes
                    if last_state != "paused":
                        last_state = "paused"
                        return ("paused", "paused")
                    ev = None
                    with _CONVERT_LOCK:
                        c = _CONVERT_CTRL.get(convert_id)
                        ev = c["event"] if c else None
                    if ev:
                        ev.wait(timeout=0.5)

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_map = {executor.submit(work): idx for idx, _, work in units}
                for fut in as_completed(future_map):
                    slot = future_map[fut]
                    try:
                        sidx, out = fut.result()
                    except Exception:
                        out = []
                        sidx = slot
                    results[sidx] = out
                    done += 1

                    # After each completion, honour pause/stop.
                    signal, st = check_control()
                    if signal == "paused":
                        yield sse({"_paused": True, "done": done, "total": total})
                        # Spin until resumed or stopped.
                        while True:
                            signal2, st2 = check_control()
                            if signal2 == "resumed":
                                yield sse({"_resumed": True, "done": done, "total": total})
                                break
                            if signal2 == "stopped":
                                break
                        if st2 == "stopped":
                            stopped = True
                            break
                    elif signal == "stopped":
                        stopped = True
                        break

                    if stopped:
                        break

                    yield sse({"_lines": True, "lines": out, "done": done, "total": total})
                    if done >= total:
                        break

            # Assemble in original order from whatever completed.
            ordered = ["#EXTM3U"]
            for idx, _w, _work in units:
                if idx in results:
                    ordered.extend(results[idx])
            payload = "\n".join(ordered) + "\n"

            # Clean up control state.
            with _CONVERT_LOCK:
                _CONVERT_CTRL.pop(convert_id, None)

            if stopped:
                yield sse({"_stopped": True, "total": total, "done": done, "file": payload})
            else:
                yield sse({"_done": True, "total": total, "done": done, "file": payload})
        except GeneratorExit:
            with _CONVERT_LOCK:
                _CONVERT_CTRL.pop(convert_id, None)
            raise

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"},
    )


@app.route("/api/resolve", methods=["POST"])
def resolve():
    cmd = (data.get("cmd","") or data.get("series_cmd","")).strip() if 'data' in locals() else (request.json.get("cmd","") or request.json.get("series_cmd","")).strip()
    portal_url   = request.json.get("portal_url","").strip()
    mac          = request.json.get("mac","").strip().upper()
    content_type = request.json.get("content_type","live")
    series_id    = request.json.get("series_id","").strip()

    if not portal_url or not mac:
        return jsonify({"ok":False,"error":"Missing portal_url or mac"}),400

    ctx = get_token(portal_url, mac)
    if not ctx:
        return jsonify({"ok":False,"error":"Auth failed"}),400
    auth_session(ctx)

    season_num   = int(request.json.get("season_num", 0) or 0)
    episode_num  = int(request.json.get("episode_num", 0) or 0)

    if not cmd and series_id and content_type == "series":
        cmd = build_series_cmd(series_id, season_num or 1)

    if not cmd:
        return jsonify({"ok":False,"error":"No stream command found for this item"}),400

    url = resolve_stream_url(ctx, cmd, content_type=content_type, series_id=series_id, season_num=season_num or 1, episode_num=episode_num or 1)
    if not url:
        return jsonify({"ok":False,"error":"Could not resolve stream URL for this item"}),400
    return jsonify({"ok":True,"url":url})


# ─── Portals JSON file ────────────────────────────────────────────────────────
PORTALS_FILE = os.path.join(os.path.dirname(__file__), "portals.json")

def read_portals():
    try:
        with open(PORTALS_FILE) as f:
            return json.load(f)
    except:
        return []

def write_portals(data):
    with open(PORTALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


@app.route("/api/portals", methods=["GET"])
def portals_get():
    return jsonify({"ok": True, "portals": read_portals()})


@app.route("/api/portals", methods=["POST"])
def portals_save():
    data = request.json
    url  = data.get("url","").strip()
    mac  = data.get("mac","").strip().upper()
    if not url or not mac:
        return jsonify({"ok":False,"error":"url and mac required"}),400
    try:
        from urllib.parse import urlparse as _up
        label = _up(url).hostname or url
    except:
        label = url
    portals = read_portals()
    if not any(p["url"]==url and p["mac"]==mac for p in portals):
        portals.append({"url":url,"mac":mac,"label":label})
        write_portals(portals)
    return jsonify({"ok":True,"portals":portals})


@app.route("/api/portals/<int:idx>", methods=["DELETE"])
def portals_delete(idx):
    portals = read_portals()
    if 0 <= idx < len(portals):
        portals.pop(idx)
        write_portals(portals)
    return jsonify({"ok":True,"portals":portals})


@app.route("/api/proxy_stream")
def proxy_stream():
    portal_url   = request.args.get("portal_url","").strip()
    mac          = request.args.get("mac","").strip().upper()
    cmd          = request.args.get("cmd","").strip()
    content_type = request.args.get("type","live")

    if not portal_url or not mac or not cmd:
        return "Missing fields", 400

    ctx = get_token(portal_url, mac)
    if not ctx:
        return "Auth failed", 401
    auth_session(ctx)

    stream_url = resolve_stream_url(ctx, cmd, content_type=content_type)
    if not stream_url:
        return "No stream URL", 404

    try:
        headers = dict(ctx["session"].headers)
        headers.update({
            "User-Agent": MAG_UA,
            "Referer": ctx["base_url"],
            "Origin":  ctx["base_url"].rstrip("/"),
        })
        upstream = requests.get(stream_url, headers=headers, stream=True, timeout=15)

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        resp_headers = {}
        for h in ["Content-Type","Content-Length","Transfer-Encoding"]:
            if h in upstream.headers:
                resp_headers[h] = upstream.headers[h]

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=resp_headers,
        )
    except Exception as e:
        return str(e), 500

@app.route("/api/add/m3u-file", methods=["POST"])
def add_m3u_file():
    f = request.files.get("file")
    name = request.form.get("name", "My Playlist")
    if not f:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    safe = re.sub(r'[^\w\s-]', "", name).strip() or "unnamed"
    path = os.path.join("PlayLists", "M3U", f"{safe}.m3u")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f.save(path)
    count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip() and not line.startswith("#"):
                count += 1
    return jsonify({"ok": True, "count": count, "name": safe})

PLAYLIST_DIR = os.path.join(os.path.dirname(__file__), "PlayLists")

def _scan_json_playlists():
    """Scan PlayLists/ for .json playlist files."""
    pls = []
    if not os.path.isdir(PLAYLIST_DIR):
        return pls
    for fn in sorted(os.listdir(PLAYLIST_DIR)):
        if not fn.lower().endswith(".json"):
            continue
        fp = os.path.join(PLAYLIST_DIR, fn)
        try:
            with open(fp, "r") as f:
                data = json.load(f)
            if "channels" in data:
                ch_count = len(data.get("channels", []))
            else:
                ch_count = _count_channels(data)
            pls.append({
                "id": fn[:-5],
                "name": data.get("name", fn[:-5]),
                "list_name": data.get("name", fn[:-5]),
                "source": data.get("source", "portal"),
                "channels": ch_count,
                "channel_count": ch_count,
                "updated": data.get("updated", 0),
                "portal_url": data.get("portal_url", ""),
                "mac": data.get("mac", ""),
            })
        except Exception:
            pass
    return pls

def _find_playlist_file(pl_id):
    """Find a playlist file by id in PlayLists/."""
    safe = re.sub(r'[^\w\s.-]', "", pl_id).strip()
    fp = os.path.join(PLAYLIST_DIR, f"{safe}.json")
    return fp if os.path.isfile(fp) else None

def _count_channels(data):
    """Count total playable items from nested category structure."""
    count = 0
    for cat in data.get("categories", {}).get("live", []):
        count += len(cat.get("Channel", []))
    for cat in data.get("categories", {}).get("vod", []):
        count += len(cat.get("Movie", []))
    for cat in data.get("categories", {}).get("series", []):
        for s_entry in cat.get("series", []):
            count += len(s_entry.get("episode", []))
    return count

def _reconstruct_channels(data):
    """Reconstruct flat channels array from nested category structure."""
    chs = []
    pu = data.get("portal_url", "")
    mc = data.get("mac", "")
    for cat in data.get("categories", {}).get("live", []):
        for ch in cat.get("Channel", []):
            ch.setdefault("group", cat["name"])
            ch.setdefault("content_type", "live")
            ch.setdefault("needsResolve", True)
            ch.setdefault("portal_url", pu)
            ch.setdefault("mac", mc)
            if ch.get("cmd") and not ch.get("url"):
                ch["url"] = ch["cmd"]
            chs.append(ch)
    for cat in data.get("categories", {}).get("vod", []):
        for ch in cat.get("Movie", []):
            ch.setdefault("group", cat["name"])
            ch.setdefault("content_type", "vod")
            ch.setdefault("needsResolve", True)
            ch.setdefault("portal_url", pu)
            ch.setdefault("mac", mc)
            if ch.get("cmd") and not ch.get("url"):
                ch["url"] = ch["cmd"]
            chs.append(ch)
    for cat in data.get("categories", {}).get("series", []):
        for s_entry in cat.get("series", []):
            sname = s_entry.get("name", "")
            for ep in s_entry.get("episode", []):
                sn = ep.get("season_num", 1)
                en = ep.get("episode_num", 1)
                chs.append({
                    "name": f"{sname} S{sn:02d} E{en:02d}",
                    "series_id": s_entry.get("id"),
                    "season_num": sn,
                    "episode_num": en,
                    "cmd": ep.get("cmd", ""),
                    "url": ep.get("cmd", ""),
                    "group": cat["name"],
                    "content_type": "series",
                    "needsResolve": True,
                    "portal_url": ep.get("portal_url", pu),
                    "mac": ep.get("mac", mc),
                })
    return chs

def _clean_categories(data):
    """Return category metadata without nested channel/episode data."""
    result = {}
    for ct in ("live", "vod", "series"):
        result[ct] = []
        for cat in data.get("categories", {}).get(ct, []):
            clean = {"id": cat.get("id"), "name": cat.get("name"), "cached": cat.get("cached", False), "count": cat.get("count", 0)}
            if ct == "series":
                entries = cat.get("series", [])
                if entries and "cmd" in entries[0]:
                    clean["_flat"] = True
                clean["series"] = [{"id": s.get("id"), "name": s.get("name")} for s in entries]
            result[ct].append(clean)
    return result

@app.route("/api/playlists", methods=["GET"])
def api_get_playlists():
    pls = _scan_json_playlists()
    return jsonify({"playlists": pls})

@app.route("/api/playlists/<pl_id>", methods=["DELETE"])
def api_delete_playlist(pl_id):
    fp = _find_playlist_file(pl_id)
    if not fp:
        return jsonify({"error": "Playlist not found"}), 404
    os.remove(fp)
    return jsonify({"ok": True})

@app.route("/api/playlists/<pl_id>/channels", methods=["GET"])
def api_get_playlist_channels(pl_id):
    fp = _find_playlist_file(pl_id)
    if not fp:
        return jsonify({"error": "Playlist not found"}), 404
    try:
        with open(fp, "r") as f:
            data = json.load(f)
        # Backward compat: detect old format by presence of "channels" key
        if "channels" in data:
            chs = data.get("channels", [])
            # If channels is empty but categories have nested data, migrate
            if not chs and any(data.get("categories", {}).get(ct, []) for ct in ("live","vod","series")):
                chs = _reconstruct_channels(data)
                cats = _clean_categories(data)
                data.pop("channels", None)
                try:
                    with open(fp, "w") as f:
                        json.dump(data, f, indent=2)
                except Exception:
                    pass
            else:
                for ch in chs:
                    if not ch.get("group") and ch.get("genre_id"):
                        ct = ch.get("content_type", "live")
                        for cat in data.get("categories", {}).get(ct, []):
                            if str(cat.get("id")) == str(ch["genre_id"]):
                                ch["group"] = cat.get("name", "All")
                                break
                    if not ch.get("group"):
                        ch["group"] = ch.get("genre") or "All"
                cats = data.get("categories", {})
        else:
            chs = _reconstruct_channels(data)
            cats = _clean_categories(data)
        groups = {}
        for ch in chs:
            g = ch.get("group", "All")
            groups[g] = groups.get(g, 0) + 1
        return jsonify({
            "channels": chs,
            "groups": groups,
            "portal_url": data.get("portal_url", ""),
            "mac": data.get("mac", ""),
            "source": data.get("source", ""),
            "categories": cats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    if not os.path.isfile(SETTINGS_PATH):
        return jsonify({})
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})

@app.route("/api/settings", methods=["PUT"])
def api_put_settings():
    data = request.get_json(silent=True) or {}
    existing = {}
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    existing.update(data)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    return jsonify(existing)

def _convert_to_json(portal_url, mac, channels):
    hostname = urlparse(portal_url).hostname or "unknown"
    safe_host = re.sub(r'[^\w.-]', '', hostname)
    safe_mac = mac.replace(':', '')[:12]
    filename = f"{safe_host}_{safe_mac}.json"
    filepath = os.path.join(PLAYLIST_DIR, filename)

    new_entries = []
    for ch in channels:
        cmd = ch.get("cmd", "")
        if cmd.startswith("ffmpeg "):
            cmd = cmd[7:]
        entry = {
            "name": ch.get("name", "Unknown"),
            "cmd": cmd,
            "group": ch.get("genre", "All"),
            "content_type": ch.get("content_type", "live"),
        }
        if ch.get("content_type") == "series":
            entry["series_id"] = ch.get("id", "")
            entry["episodes"] = ch.get("episodes", [])
        new_entries.append(entry)

    existing = []
    if os.path.isfile(filepath):
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
                existing = data.get("channels", [])
        except Exception:
            pass

    existing_cmds = {e["cmd"] for e in existing if e.get("cmd")}
    merged = existing + [e for e in new_entries if e["cmd"] not in existing_cmds]

    os.makedirs(PLAYLIST_DIR, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump({
            "name": safe_host,
            "source": "portal",
            "portal_url": portal_url,
            "mac": mac,
            "updated": time.time(),
            "channels": merged,
        }, f, indent=2)

    def gen():
        yield f"data: {json.dumps({'_start': True, 'total': len(new_entries), 'filename': filename})}\n\n"
        for i, e in enumerate(new_entries):
            yield f"data: {json.dumps({'_lines': True, 'lines': [e], 'done': i+1, 'total': len(new_entries)})}\n\n"
        yield f"data: {json.dumps({'_done': True, 'filename': filename})}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/save-categories", methods=["POST"])
def save_categories():
    portal_url = (request.json.get("portal_url","") or "").strip()
    mac        = (request.json.get("mac","") or "").strip().upper()
    cats       = request.json.get("cats", {})
    if not portal_url or not mac:
        return jsonify({"ok":False,"error":"Missing portal_url or mac"}),400

    hostname = urlparse(portal_url).hostname or "unknown"
    safe_host = re.sub(r'[^\w.-]', '', hostname)
    safe_mac = mac.replace(':', '')[:12]
    filename = f"{safe_host}_{safe_mac}.json"
    filepath = os.path.join(PLAYLIST_DIR, filename)

    # Build category skeleton with empty nested arrays
    cat_data = {}
    for ct in ("live","vod","series"):
        cat_data[ct] = []
        for c in cats.get(ct, []):
            entry = {"id":c.get("id",""),"name":c.get("name",""),"cached":False,"count":c.get("count",0)}
            if ct == "live":
                entry["Channel"] = []
            elif ct == "vod":
                entry["Movie"] = []
            elif ct == "series":
                entry["series"] = []
            cat_data[ct].append(entry)

    os.makedirs(PLAYLIST_DIR, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump({
            "name": safe_host,
            "source": "portal",
            "portal_url": portal_url,
            "mac": mac,
            "updated": time.time(),
            "categories": cat_data,
        }, f, indent=2)

    return jsonify({"ok":True, "filename": filename})

@app.route("/api/open-vlc", methods=["GET"])
def open_vlc():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Missing url"}), 400
    vlc_path = ""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings = json.load(f)
            vlc_path = settings.get("vlc_path", "")
    except Exception:
        pass
    if not vlc_path or not os.path.isfile(vlc_path):
        return jsonify({"ok": False, "error": "VLC path not configured or not found — go to Settings to set it"}), 404
    try:
        subprocess.Popen([vlc_path, url], shell=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/check-vlc-path", methods=["POST"])
def check_vlc_path():
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "No path provided"}), 400
    if os.path.isfile(path):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "File not found at this path"})

@app.route("/api/playlists/favorites/add-category", methods=["POST"])
def add_category_to_favorites():
    data = request.json
    pl_type = data.get("type", "live")
    cat_name = data.get("cat_name", "")
    cat_id = data.get("cat_id", "")
    channels = data.get("channels", [])
    src_id = data.get("source_playlist_id", "")

    if not cat_name or not channels:
        return jsonify({"ok": False, "error": "Missing cat_name or channels"}), 400

    fp = os.path.join(PLAYLIST_DIR, "Favorites.json")
    os.makedirs(PLAYLIST_DIR, exist_ok=True)

    fav = {}
    if os.path.isfile(fp):
        with open(fp, "r") as f:
            fav = json.load(f)

    if not fav.get("categories"):
        fav = {"name": "Favorites", "source": "portal", "updated": time.time(), "categories": {"live": [], "vod": [], "series": []}}

    # For series with series_name, treat as individual series entry
    series_name = data.get("series_name", "") or data.get("series_id", "") or ""

    # Find or create category entry
    cat_entry = None
    cats = fav["categories"].get(pl_type, [])
    for c in cats:
        if c.get("name") == cat_name:
            cat_entry = c
            break
    if not cat_entry:
        cat_entry = {"id": cat_id, "name": cat_name, "cached": True, "count": 0}
        if pl_type == "live":
            cat_entry["Channel"] = []
        elif pl_type == "vod":
            cat_entry["Movie"] = []
        elif pl_type == "series":
            cat_entry["series"] = []
        cats.append(cat_entry)
        fav["categories"][pl_type] = cats

    # Merge channels (dedupe by id)
    added = 0

    if pl_type == "series" and series_name:
        # Remove cat_count for now; will be set below
        # Find or create series entry inside category
        series_entries = cat_entry.get("series", [])
        s_entry = None
        for s in series_entries:
            if s.get("name") == series_name:
                s_entry = s
                break
        if not s_entry:
            s_entry = {"id": data.get("series_id", ""), "name": series_name, "episode": []}
            series_entries.append(s_entry)
        # Merge episodes (dedupe by season_num+episode_num)
        existing_eps = s_entry.get("episode", [])
        existing_keys = {(ep.get("season_num"), ep.get("episode_num")) for ep in existing_eps if ep.get("season_num") is not None}
        for ch in channels:
            sn = ch.get("season_num")
            en = ch.get("episode_num")
            key = (sn, en)
            if key in existing_keys:
                continue
            existing_eps.append({
                "season_num": sn,
                "episode_num": en,
                "cmd": ch.get("cmd", ""),
                "portal_url": ch.get("portal_url", ""),
                "mac": ch.get("mac", ""),
            })
            if key != (None, None):
                existing_keys.add(key)
            added += 1
        s_entry["episode"] = existing_eps
        cat_entry["series"] = series_entries
        cat_entry["count"] = len(series_entries)
    else:
        if pl_type == "series":
            # Group channels by series_id into proper series entries
            groups = {}
            for ch in channels:
                sid = ch.get("series_id", "") or ""
                if not sid:
                    sid = "__unknown__"
                if sid not in groups:
                    sname = ch.get("series_name", "")
                    if not sname:
                        sname = re.sub(r'\s+S\d{2,4}\s+E\d{2,4}(?:\s|$)', '', ch.get("name", "")).strip()
                    groups[sid] = {"id": sid if sid != "__unknown__" else "", "name": sname or ch.get("name", ""), "episode": []}
                groups[sid]["episode"].append({
                    "season_num": ch.get("season_num", 0),
                    "episode_num": ch.get("episode_num", 0),
                    "cmd": ch.get("cmd", ""),
                    "portal_url": ch.get("portal_url", ""),
                    "mac": ch.get("mac", ""),
                })
            series_entries = cat_entry.get("series", [])
            existing_map = {s.get("id", ""): s for s in series_entries}
            for sid, s_entry in groups.items():
                key = sid if sid != "__unknown__" else ""
                if key in existing_map:
                    es = existing_map[key]
                    existing_eps = es.get("episode", [])
                    existing_keys = {(ep.get("season_num"), ep.get("episode_num")) for ep in existing_eps}
                    for ep in s_entry["episode"]:
                        k = (ep.get("season_num"), ep.get("episode_num"))
                        if k not in existing_keys:
                            existing_eps.append(ep)
                            existing_keys.add(k)
                            added += 1
                    es["episode"] = existing_eps
                else:
                    s_entry["episode"] = s_entry["episode"]
                    series_entries.append(s_entry)
                    added += len(s_entry["episode"])
            cat_entry["series"] = series_entries
            cat_entry["count"] = len(series_entries)
        else:
            container_key = {"live": "Channel", "vod": "Movie", "series": "series"}.get(pl_type, "Channel")
            existing = cat_entry.get(container_key, [])
            existing_ids = {ch.get("id", "") for ch in existing if ch.get("id")}
            for ch in channels:
                ch_id = ch.get("id", "")
                if ch_id and ch_id in existing_ids:
                    continue
                entry = {
                    "id": ch_id,
                    "name": ch.get("name", ""),
                    "cmd": ch.get("cmd", ""),
                    "portal_url": ch.get("portal_url", ""),
                    "mac": ch.get("mac", ""),
                }
                if pl_type in ("live", "vod"):
                    entry["group"] = cat_name
                    entry["content_type"] = pl_type
                existing.append(entry)
                if ch_id:
                    existing_ids.add(ch_id)
                added += 1
            cat_entry[container_key] = existing
            cat_entry["count"] = len(existing)
    fav["updated"] = time.time()

    with open(fp, "w") as f:
        json.dump(fav, f, indent=2)

    return jsonify({"ok": True, "added": added})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

