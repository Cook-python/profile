import os
import re
import json
import math
import time
import random
import argparse
import datetime
import traceback

import requests

try:
    import scratchattach as sa
except Exception:
    sa = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


USERNAME = os.environ.get("SCRATCH_USERNAME", "weather_sunny")
SESSION_ID = os.environ.get("SCRATCH_SESSION_ID", "")
AREA_CODE = os.environ.get("JMA_AREA_CODE", "130000")
AREA_LABEL = os.environ.get("AREA_LABEL", "東京")
LAT = float(os.environ.get("LATITUDE", "35.68"))
LON = float(os.environ.get("LONGITUDE", "139.77"))
BIO_FIXED = os.environ.get("BIO_FIXED", "天気と防災が好きです")
NOTICE_TEXT = os.environ.get("NOTICE_TEXT", "pythonによって自動更新されています")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))
MIN_REFRESH = int(os.environ.get("MIN_REFRESH", "1800"))
PRESENCE_WINDOW = int(os.environ.get("PRESENCE_WINDOW", "900"))
PRESENCE_ONLINE = os.environ.get("PRESENCE_ONLINE", "オンライン")
PRESENCE_OFFLINE = os.environ.get("PRESENCE_OFFLINE", "オフライン")
UPDATE_BIO = os.environ.get("UPDATE_BIO", "0") == "1"
NEWLINE_COST = int(os.environ.get("NEWLINE_COST", "2"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

PROFILE_LIMIT = 200
SEP = "\n"
TZ = datetime.timezone(datetime.timedelta(hours=9))
UA = "scratch-profile-bot/1.0 (+https://scratch.mit.edu/users/%s/)" % USERNAME

FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/%s.json" % AREA_CODE
WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/warning/%s.json" % AREA_CODE
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
QUAKE_URL = "https://api.p2pquake.net/v2/history?codes=551&limit=30"
TSUNAMI_URL = "https://api.p2pquake.net/v2/history?codes=552&limit=1"
SCRATCH_FOLLOWERS_URL = "https://api.scratch.mit.edu/users/%s/followers/?limit=1" % USERNAME
SCRATCH_MESSAGES_URL = "https://api.scratch.mit.edu/users/%s/messages/count/" % USERNAME
SCRATCH_PROJECTS_URL = "https://api.scratch.mit.edu/users/%s/projects/" % USERNAME

WARNING_NAMES = {
    "02": "暴風雪警報", "03": "大雨警報", "04": "洪水警報", "05": "暴風警報",
    "06": "大雪警報", "07": "波浪警報", "08": "高潮警報",
    "10": "大雨注意報", "12": "大雪注意報", "13": "風雪注意報", "14": "雷注意報",
    "15": "強風注意報", "16": "波浪注意報", "17": "融雪注意報", "18": "洪水注意報",
    "19": "高潮注意報", "20": "濃霧注意報", "21": "乾燥注意報", "22": "なだれ注意報",
    "23": "低温注意報", "24": "霜注意報", "25": "着氷注意報", "26": "着雪注意報",
    "27": "その他注意報",
    "32": "暴風雪特別警報", "33": "大雨特別警報", "35": "暴風特別警報",
    "36": "大雪特別警報", "37": "波浪特別警報", "38": "高潮特別警報",
}
SPECIAL_CODES = {"32", "33", "35", "36", "37", "38"}
ALERT_CODES = {"02", "03", "04", "05", "06", "07", "08"}

SCALE_NAMES = {
    10: "1", 20: "2", 30: "3", 40: "4", 45: "5弱", 46: "5弱以上",
    50: "5強", 55: "6弱", 60: "6強", 70: "7",
}
MOON_NAMES = ["新月", "三日月", "上弦", "十三夜", "満月", "寝待月", "下弦", "有明月"]

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(msg):
    print("[%s] %s" % (datetime.datetime.now(TZ).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def clen(text):
    return len(text or "")


def budget_len(text):
    text = text or ""
    return len(text) + text.count("\n") * (NEWLINE_COST - 1)


def assemble(blocks, limit=PROFILE_LIMIT, sep=SEP):
    items = [b for b in blocks if b.get("t")]
    while True:
        text = sep.join(b["t"] for b in items)
        if budget_len(text) <= limit:
            return text
        droppable = [b for b in items if b.get("p", 1) > 0]
        if not droppable:
            return text[:limit]
        items.remove(max(droppable, key=lambda b: b.get("p", 1)))


def compact(text):
    if not text:
        return ""
    text = re.sub(r"\s+", "", str(text))
    return text.replace("後", "のち").replace("一時", "時々")


def short_number(n):
    try:
        n = int(n)
    except Exception:
        return "0"
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 10000:
        return "%.1fw" % (n / 10000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


class Backoff:
    def __init__(self):
        self.fails = {}
        self.next_at = {}

    def ready(self, key):
        return time.time() >= self.next_at.get(key, 0.0)

    def ok(self, key):
        self.fails[key] = 0
        self.next_at[key] = 0.0

    def fail(self, key):
        n = self.fails.get(key, 0) + 1
        self.fails[key] = n
        delay = min(1800.0, 30.0 * (2 ** (n - 1))) * (0.8 + random.random() * 0.4)
        self.next_at[key] = time.time() + delay
        return delay


BACKOFF = Backoff()
CACHE = {}


def guarded(key, interval, fn, default=None):
    now = time.time()
    entry = CACHE.get(key)
    if entry and now - entry[0] < interval:
        return entry[1]
    if not BACKOFF.ready(key):
        return entry[1] if entry else default
    try:
        value = fn()
        BACKOFF.ok(key)
        CACHE[key] = (now, value)
        return value
    except Exception as exc:
        delay = BACKOFF.fail(key)
        log("fetch fail %s: %s (next in %.0fs)" % (key, exc, delay))
        return entry[1] if entry else default


def get_json(url, timeout=12):
    res = HTTP.get(url, timeout=timeout)
    res.raise_for_status()
    return res.json()


def fetch_forecast():
    data = get_json(FORECAST_URL)
    out = {"weather": "", "pop": None, "temp_max": None, "temp_min": None}
    for s in data[0].get("timeSeries", []):
        areas = s.get("areas", [])
        if not areas:
            continue
        a = areas[0]
        if "weathers" in a and not out["weather"]:
            out["weather"] = compact(a["weathers"][0])
        if "pops" in a and out["pop"] is None:
            vals = [v for v in a["pops"] if v not in ("", None)]
            if vals:
                out["pop"] = int(vals[0])
        if "temps" in a and out["temp_max"] is None:
            vals = [v for v in a["temps"] if v not in ("", None)]
            if len(vals) >= 2:
                out["temp_min"] = int(vals[0])
                out["temp_max"] = int(vals[-1])
            elif vals:
                out["temp_max"] = int(vals[0])
    if len(data) > 1:
        for s in data[1].get("timeSeries", []):
            for a in s.get("areas", []):
                if out["temp_max"] is None and "tempsMax" in a:
                    vals = [v for v in a["tempsMax"] if v not in ("", None)]
                    if vals:
                        out["temp_max"] = int(vals[0])
    return out


def fetch_warnings():
    data = get_json(WARNING_URL)
    codes = set()
    for at in data.get("areaTypes", []):
        for area in at.get("areas", []):
            for w in area.get("warnings", []):
                if w.get("status", "") in ("解除", "None", ""):
                    continue
                code = str(w.get("code", "")).zfill(2)
                if code in WARNING_NAMES:
                    codes.add(code)
    return {
        "special": sorted(codes & SPECIAL_CODES),
        "alert": sorted(codes & ALERT_CODES),
        "advisory": sorted(codes - SPECIAL_CODES - ALERT_CODES),
    }


def fetch_openmeteo():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current": "temperature_2m,relative_humidity_2m,precipitation",
        "minutely_15": "precipitation",
        "forecast_days": 1,
        "timezone": "Asia/Tokyo",
    }
    res = HTTP.get(OPENMETEO_URL, params=params, timeout=12)
    res.raise_for_status()
    data = res.json()
    cur = data.get("current", {})
    out = {"temp": cur.get("temperature_2m"), "rain_soon": 0.0, "rain_in": None}
    minutely = data.get("minutely_15", {})
    now = datetime.datetime.now(TZ).replace(tzinfo=None)
    total = 0.0
    first_at = None
    for t, v in zip(minutely.get("time", []), minutely.get("precipitation", [])):
        try:
            ts = datetime.datetime.fromisoformat(t)
        except Exception:
            continue
        delta = (ts - now).total_seconds()
        if 0 <= delta <= 3600:
            v = float(v or 0.0)
            total += v
            if v >= 0.2 and first_at is None:
                first_at = int(delta // 60)
    out["rain_soon"] = round(total, 1)
    out["rain_in"] = first_at
    return out


def fetch_quakes():
    data = get_json(QUAKE_URL)
    today = datetime.datetime.now(TZ).strftime("%Y/%m/%d")
    latest = None
    count_today = 0
    for item in data:
        eq = item.get("earthquake") or {}
        t = eq.get("time", "")
        scale = eq.get("maxScale", -1)
        if latest is None and scale and scale >= 10:
            hyp = eq.get("hypocenter") or {}
            latest = {
                "id": item.get("id", ""),
                "time": t,
                "place": compact(hyp.get("name", "")) or "調査中",
                "mag": hyp.get("magnitude", -1),
                "depth": hyp.get("depth", -1),
                "scale": scale,
            }
        if t.startswith(today) and scale and scale >= 10:
            count_today += 1
    return {"latest": latest, "count_today": count_today}


def fetch_tsunami():
    data = get_json(TSUNAMI_URL)
    if not data:
        return {"active": False, "grade": "", "areas": 0}
    item = data[0]
    if item.get("cancelled"):
        return {"active": False, "grade": "", "areas": 0}
    grades = [a.get("grade", "") for a in item.get("areas", [])]
    top = ""
    for g in ("MajorWarning", "Warning", "Watch", "Unknown"):
        if g in grades:
            top = g
            break
    label = {"MajorWarning": "大津波警報", "Warning": "津波警報", "Watch": "津波注意報"}.get(top, "")
    return {"active": bool(label), "grade": label, "areas": len(grades)}


def fetch_followers_count():
    try:
        res = HTTP.get("https://scratch.mit.edu/users/%s/followers/" % USERNAME,
                       timeout=12, headers={"Accept": "text/html"})
        res.raise_for_status()
        plain = re.sub(r"<[^>]+>", " ", res.text)
        plain = re.sub(r"\s+", " ", plain)
        m = re.search(r"Followers\s*\(?\s*([\d,]+)\s*\)?", plain)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    total = 0
    offset = 0
    while offset < 4000:
        data = get_json("https://api.scratch.mit.edu/users/%s/followers/?limit=40&offset=%d"
                        % (USERNAME, offset))
        total += len(data)
        if len(data) < 40:
            return total
        offset += 40
    return total


def fetch_latest_follower():
    data = get_json(SCRATCH_FOLLOWERS_URL)
    if not data:
        return ""
    return data[0].get("username", "")


def fetch_messages_count():
    data = get_json(SCRATCH_MESSAGES_URL)
    return int(data.get("count", 0))


def parse_scratch_time(value):
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def fetch_projects_stats():
    total = {"views": 0, "loves": 0, "favorites": 0, "count": 0, "last_modified": 0.0}
    offset = 0
    while offset < 120:
        data = get_json("%s?limit=40&offset=%d" % (SCRATCH_PROJECTS_URL, offset))
        if not data:
            break
        for p in data:
            st = p.get("stats") or {}
            total["views"] += int(st.get("views", 0))
            total["loves"] += int(st.get("loves", 0))
            total["favorites"] += int(st.get("favorites", 0))
            total["count"] += 1
            hist = p.get("history") or {}
            for key in ("modified", "shared", "created"):
                ts = parse_scratch_time(hist.get(key))
                if ts > total["last_modified"]:
                    total["last_modified"] = ts
        if len(data) < 40:
            break
        offset += 40
    return total


def presence_text(ts, now_ts):
    if not ts:
        return PRESENCE_OFFLINE
    if now_ts - float(ts) <= PRESENCE_WINDOW:
        return PRESENCE_ONLINE
    return PRESENCE_OFFLINE


def jd_to_local(jd):
    unix = (jd - 2440587.5) * 86400.0
    return datetime.datetime.fromtimestamp(unix, tz=datetime.timezone.utc).astimezone(TZ)


def sun_times(date, lat=LAT, lon=LON):
    rad = math.radians
    deg = math.degrees
    n = (date - datetime.date(2000, 1, 1)).days + 0.0008
    js = n - lon / 360.0
    m = (357.5291 + 0.98560028 * js) % 360.0
    c = 1.9148 * math.sin(rad(m)) + 0.02 * math.sin(rad(2 * m)) + 0.0003 * math.sin(rad(3 * m))
    lam = (m + c + 180.0 + 102.9372) % 360.0
    j_transit = 2451545.0 + js + 0.0053 * math.sin(rad(m)) - 0.0069 * math.sin(rad(2 * lam))
    decl = math.asin(math.sin(rad(lam)) * math.sin(rad(23.4397)))
    cos_w = (math.sin(rad(-0.833)) - math.sin(rad(lat)) * math.sin(decl)) / (math.cos(rad(lat)) * math.cos(decl))
    cos_w = max(-1.0, min(1.0, cos_w))
    w = deg(math.acos(cos_w))
    return jd_to_local(j_transit - w / 360.0), jd_to_local(j_transit + w / 360.0)


def moon_info(now_utc):
    ref = datetime.datetime(2000, 1, 6, 18, 14, tzinfo=datetime.timezone.utc)
    age = ((now_utc - ref).total_seconds() / 86400.0) % 29.530588853
    idx = int((age / 29.530588853) * 8 + 0.5) % 8
    return age, MOON_NAMES[idx]


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        log("state save fail: %s" % exc)


def scale_label(scale):
    return SCALE_NAMES.get(scale, "不明")


def quake_is_major(quake):
    return bool(quake and quake.get("latest") and quake["latest"].get("scale", 0) >= 45)


def emergency_text(warn, tsunami, quake):
    parts = []
    if tsunami and tsunami.get("active"):
        parts.append("【%s】直ちに高台へ" % tsunami["grade"])
    if warn and warn.get("special"):
        parts.append("【%s】命を守る行動を" % "・".join(WARNING_NAMES[c] for c in warn["special"]))
    if quake_is_major(quake):
        q = quake["latest"]
        parts.append("【最大震度%s】%s M%s" % (scale_label(q["scale"]), q["place"], q["mag"]))
    return parts


def rotation_blocks(ctx, now):
    subs = []
    sun = ctx.get("sun") or (None, None)
    if sun[0] and sun[1]:
        subs.append("日出:%s 日入:%s" % (sun[0].strftime("%H:%M"), sun[1].strftime("%H:%M")))
    moon = ctx.get("moon")
    if moon:
        subs.append("月齢:%.1f %s" % (moon[0], moon[1]))
    subs.append("本日の有感地震:%d回" % (ctx.get("quake") or {}).get("count_today", 0))
    followers = ctx.get("followers")
    fdiff = ctx.get("follower_diff")
    if followers is not None:
        if fdiff:
            subs.append("フォロワー:%d(%+d)" % (followers, fdiff))
        else:
            subs.append("フォロワー:%d" % followers)
    vdiff = ctx.get("view_diff")
    if vdiff:
        subs.append("24h再生:+%d" % vdiff)
    picked = []
    new_follower = ctx.get("new_follower")
    if new_follower:
        picked.append("新フォロワー:@%s" % new_follower)
    if subs:
        idx = (now.hour * 60 + now.minute) // 5
        picked.append(subs[idx % len(subs)])
        picked.append(subs[(idx + 1) % len(subs)])
    return picked


def build_status(ctx, now):
    stamp = "更新:%s" % now.strftime("%m/%d %H:%M")
    notice = {"t": NOTICE_TEXT, "p": 1}
    emerg = ctx.get("emergency") or []

    if emerg:
        blocks = [{"t": emerg[0], "p": 0}]
        for e in emerg[1:]:
            blocks.append({"t": e, "p": 1})
        quake = (ctx.get("quake") or {}).get("latest")
        if quake:
            blocks.append({"t": "地震:%s %s M%s 深さ%skm" % (
                quake["time"][5:16], quake["place"], quake["mag"], quake["depth"]), "p": 3})
        blocks.append({"t": stamp, "p": 0})
        blocks.append(notice)
        return assemble(blocks)

    blocks = [{"t": BIO_FIXED, "p": 0}]

    stats = ctx.get("projects")
    if stats and stats.get("count"):
        blocks.append({"t": "作品:%d 閲覧:%s 好き:%s 星:%s" % (
            stats["count"], short_number(stats["views"]),
            short_number(stats["loves"]), short_number(stats["favorites"])), "p": 0})

    if ctx.get("active_text"):
        blocks.append({"t": "状態:%s" % ctx["active_text"], "p": 0})

    if ctx.get("messages"):
        blocks.append({"t": "未読:%d" % ctx["messages"], "p": 11})

    fc = ctx.get("forecast") or {}
    om = ctx.get("openmeteo") or {}
    if fc.get("weather"):
        blocks.append({"t": "天気:%s %s" % (AREA_LABEL, fc["weather"][:14]), "p": 2})

    warn = ctx.get("warning") or {}
    if warn.get("special"):
        blocks.append({"t": "【%s】" % "・".join(WARNING_NAMES[c] for c in warn["special"]), "p": 0})
    elif warn.get("alert"):
        blocks.append({"t": "【%s】" % "・".join(WARNING_NAMES[c] for c in warn["alert"]), "p": 0})
    elif warn.get("advisory"):
        blocks.append({"t": "注意:%s" % "・".join(WARNING_NAMES[c] for c in warn["advisory"][:2]), "p": 6})

    line = []
    if om.get("temp") is not None:
        line.append("気温:%.1f℃" % float(om["temp"]))
    elif fc.get("temp_max") is not None:
        line.append("最高:%d℃" % fc["temp_max"])
    if fc.get("pop") is not None:
        line.append("降水:%d%%" % fc["pop"])
    if line:
        blocks.append({"t": " ".join(line), "p": 3})

    if om.get("rain_in") is not None:
        blocks.append({"t": "雨:約%d分後" % om["rain_in"], "p": 5})
    elif om.get("rain_soon", 0) >= 0.2:
        blocks.append({"t": "1h雨量:%.1fmm" % om["rain_soon"], "p": 7})

    tsunami = ctx.get("tsunami") or {}
    if tsunami.get("active"):
        blocks.append({"t": "【%s】" % tsunami["grade"], "p": 0})

    quake = (ctx.get("quake") or {}).get("latest")
    if quake:
        blocks.append({"t": "地震:%s %s 震度%s M%s" % (
            quake["time"][5:16], quake["place"], scale_label(quake["scale"]), quake["mag"]), "p": 4})

    for i, sub in enumerate(rotation_blocks(ctx, now)):
        blocks.append({"t": sub, "p": 8 + i * 2})

    blocks.append({"t": stamp, "p": 0})
    blocks.append(notice)
    return assemble(blocks)


def build_bio(ctx, now):
    emerg = ctx.get("emergency") or []
    if emerg:
        blocks = [{"t": emerg[0], "p": 0}]
        for e in emerg[1:]:
            blocks.append({"t": e, "p": 1})
        blocks.append({"t": BIO_FIXED, "p": 3})
        return assemble(blocks)
    fc = ctx.get("forecast") or {}
    om = ctx.get("openmeteo") or {}
    blocks = [{"t": BIO_FIXED, "p": 0}]
    if fc.get("weather"):
        blocks.append({"t": "天気:%s %s" % (AREA_LABEL, fc["weather"][:14]), "p": 1})
    if om.get("temp") is not None:
        blocks.append({"t": "気温:%.1f℃" % float(om["temp"]), "p": 2})
    return assemble(blocks)


def collect(state, now):
    ctx = {}
    ctx["forecast"] = guarded("forecast", 600, fetch_forecast, {})
    ctx["warning"] = guarded("warning", 300, fetch_warnings, {})
    ctx["openmeteo"] = guarded("openmeteo", 300, fetch_openmeteo, {})
    ctx["quake"] = guarded("quake", 60, fetch_quakes, {})
    ctx["tsunami"] = guarded("tsunami", 60, fetch_tsunami, {})
    ctx["messages"] = guarded("messages", 300, fetch_messages_count, 0)
    ctx["projects"] = guarded("projects", 900, fetch_projects_stats, {})
    ctx["latest_follower"] = guarded("follower", 300, fetch_latest_follower, "")
    ctx["followers"] = guarded("followers", 600, fetch_followers_count, state.get("followers"))

    ctx["sun"] = sun_times(now.date())
    ctx["moon"] = moon_info(now.astimezone(datetime.timezone.utc))

    prev_followers = state.get("followers")
    if ctx["followers"] is not None and prev_followers is not None:
        ctx["follower_diff"] = ctx["followers"] - prev_followers
    else:
        ctx["follower_diff"] = 0

    views = (ctx.get("projects") or {}).get("views")
    base = state.get("views_base")
    if views is not None:
        if base is None or time.time() - state.get("views_base_at", 0) > 86400:
            state["views_base"] = views
            state["views_base_at"] = time.time()
            ctx["view_diff"] = 0
        else:
            ctx["view_diff"] = max(0, views - base)

    now_ts = time.time()
    last_active = float(state.get("last_active_at", 0) or 0)
    msgs = ctx.get("messages")
    prev_msgs = state.get("messages_prev")
    if msgs is not None:
        if prev_msgs is not None and msgs < prev_msgs:
            last_active = now_ts
        state["messages_prev"] = msgs
    proj_modified = float((ctx.get("projects") or {}).get("last_modified", 0) or 0)
    if proj_modified > last_active:
        last_active = proj_modified
    if last_active:
        state["last_active_at"] = last_active
    ctx["active_text"] = presence_text(last_active, now_ts)

    latest = ctx.get("latest_follower")
    if latest and latest != state.get("last_follower"):
        state["last_follower"] = latest
        state["new_follower_at"] = now_ts
    if state.get("new_follower_at") and now_ts - state["new_follower_at"] <= 600:
        ctx["new_follower"] = state.get("last_follower")

    ctx["emergency"] = emergency_text(ctx.get("warning"), ctx.get("tsunami"), ctx.get("quake"))
    return ctx


def get_scratch_user():
    if DRY_RUN:
        return None
    if sa is None:
        raise RuntimeError("scratchattach is not installed")
    if not SESSION_ID:
        raise RuntimeError("SCRATCH_SESSION_ID is empty")
    session = None
    for attempt in (
        lambda: sa.login_by_id(SESSION_ID, username=USERNAME),
        lambda: sa.Session(SESSION_ID, username=USERNAME),
    ):
        try:
            session = attempt()
            break
        except Exception:
            continue
    if session is None:
        raise RuntimeError("scratchattach login failed")
    for attempt in (
        lambda: session.connect_linked_user(),
        lambda: session.get_linked_user(),
        lambda: session.connect_user(USERNAME),
    ):
        try:
            return attempt()
        except Exception:
            continue
    raise RuntimeError("cannot resolve linked user")


def set_bio(user, text):
    if DRY_RUN or user is None:
        log("dry-run bio(%d): %s" % (clen(text), text.replace("\n", " | ")))
        return True
    try:
        user.set_bio(text)
        return True
    except Exception as exc:
        log("set_bio fail: %s" % exc)
        return False


def set_status(user, text):
    if DRY_RUN or user is None:
        log("dry-run wiwo(%d): %s" % (clen(text), text.replace("\n", " | ")))
        return True
    try:
        try:
            user.set_wiwo(text)
        except AttributeError:
            user.set_work(text)
        return True
    except Exception as exc:
        log("set_wiwo fail: %s" % exc)
        return False


def run_once(state, user):
    now = datetime.datetime.now(TZ).replace(second=0, microsecond=0)
    ctx = collect(state, now)
    status = build_status(ctx, now)
    stamp = time.time()
    wrote = []

    if UPDATE_BIO:
        bio = build_bio(ctx, now)
        if bio != state.get("last_bio") or stamp - state.get("last_bio_at", 0) > MIN_REFRESH:
            if set_bio(user, bio):
                state["last_bio"] = bio
                state["last_bio_at"] = stamp
                wrote.append("bio=%d" % clen(bio))

    if status != state.get("last_status") or stamp - state.get("last_status_at", 0) > MIN_REFRESH:
        if set_status(user, status):
            state["last_status"] = status
            state["last_status_at"] = stamp
            wrote.append("wiwo=%d" % clen(status))

    if ctx.get("followers") is not None:
        state["followers"] = ctx["followers"]
    latest = (ctx.get("quake") or {}).get("latest")
    if latest:
        state["last_quake_id"] = latest.get("id", "")

    log("updated %s" % " ".join(wrote) if wrote else "skip (no change)")
    save_state(state)


def main_loop():
    state = load_state()
    user = None
    while True:
        try:
            if user is None:
                user = get_scratch_user()
                if user is not None:
                    log("logged in as %s" % USERNAME)
            run_once(state, user)
        except Exception as exc:
            log("loop error: %s" % exc)
            traceback.print_exc()
            user = None
        time.sleep(LOOP_SECONDS)


DUMMY_PROJECTS = {"views": 12840, "loves": 962, "favorites": 731, "count": 18, "last_modified": 0.0}


def dummy_ctx(mode, now):
    ctx = {
        "forecast": {"weather": "晴れ時々くもり", "pop": 30, "temp_max": 31, "temp_min": 24},
        "openmeteo": {"temp": 28.4, "rain_soon": 0.0, "rain_in": None},
        "warning": {"special": [], "alert": [], "advisory": ["14", "21"]},
        "quake": {"latest": {
            "id": "x1", "time": "2026/09/02 03:11:00", "place": "茨城県沖",
            "mag": 3.8, "depth": 40, "scale": 20}, "count_today": 2},
        "tsunami": {"active": False, "grade": "", "areas": 0},
        "projects": dict(DUMMY_PROJECTS),
        "messages": 7,
        "active_text": PRESENCE_ONLINE,
        "followers": 1284,
        "follower_diff": 3,
        "view_diff": 126,
        "new_follower": "sunny_dev",
        "sun": sun_times(now.date()),
        "moon": moon_info(now.astimezone(datetime.timezone.utc)),
    }
    if mode == "warning":
        ctx["warning"] = {"special": [], "alert": ["03", "04"], "advisory": ["14"]}
        ctx["openmeteo"] = {"temp": 24.1, "rain_soon": 8.5, "rain_in": 15}
        ctx["forecast"]["weather"] = "雨のち雷雨"
        ctx["forecast"]["pop"] = 90
    if mode == "stress":
        ctx["warning"] = {
            "special": ["33", "35", "36"],
            "alert": ["03", "04", "05", "06", "07", "08"],
            "advisory": sorted(set(WARNING_NAMES) - SPECIAL_CODES - ALERT_CODES),
        }
        ctx["forecast"]["weather"] = "非常に激しい雨のち雷を伴い所により雪または雨で暴風雪"
        ctx["quake"] = {"latest": {
            "id": "z1", "time": "2026/09/02 14:02:00",
            "place": "三陸沖および北海道南西沖から東北地方太平洋沿岸",
            "mag": 8.4, "depth": 10, "scale": 70}, "count_today": 137}
        ctx["tsunami"] = {"active": True, "grade": "大津波警報", "areas": 22}
        ctx["projects"] = {"views": 98765432, "loves": 1234567, "favorites": 987654,
                           "count": 999, "last_modified": 0.0}
        ctx["active_text"] = PRESENCE_OFFLINE
        ctx["new_follower"] = "very_long_scratch_user_name_2026"
    if mode == "quake":
        ctx["quake"] = {"latest": {
            "id": "x9", "time": "2026/09/02 14:02:00", "place": "東京湾",
            "mag": 6.1, "depth": 30, "scale": 55}, "count_today": 9}
        ctx["tsunami"] = {"active": True, "grade": "津波注意報", "areas": 4}
    ctx["emergency"] = emergency_text(ctx["warning"], ctx["tsunami"], ctx["quake"])
    return ctx


EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF←-⯿☀-➿️]")


def run_offline_tests():
    now = datetime.datetime(2026, 9, 2, 14, 35, tzinfo=TZ)
    failures = 0
    cases = (("normal", "平常時"), ("warning", "警報時"), ("quake", "地震発生時"), ("stress", "過負荷テスト"))
    for mode, label in cases:
        ctx = dummy_ctx(mode, now)
        text = build_status(ctx, now)
        print("=== %s === %d文字 (改行2文字換算で%d)" % (label, clen(text), budget_len(text)))
        print(text)
        print("")
        if budget_len(text) > PROFILE_LIMIT:
            failures += 1
            print("NG %s が上限超過" % label)
        if EMOJI_RE.search(text):
            failures += 1
            print("NG %s に絵文字" % label)
        if NOTICE_TEXT not in text:
            failures += 1
            print("NG %s に注意書きなし" % label)

    for i in range(0, 1440, 5):
        t = datetime.datetime(2026, 9, 2, 0, 0, tzinfo=TZ) + datetime.timedelta(minutes=i)
        for mode, _ in cases:
            text = build_status(dummy_ctx(mode, t), t)
            if budget_len(text) > PROFILE_LIMIT or EMOJI_RE.search(text) or NOTICE_TEXT not in text:
                failures += 1
    print("全時刻スイープ(288刻み x 4パターン): %s" % ("OK" if failures == 0 else "NG %d" % failures))
    return failures == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.test:
        raise SystemExit(0 if run_offline_tests() else 1)
    if args.once:
        state = load_state()
        user = None if DRY_RUN else get_scratch_user()
        run_once(state, user)
        return
    main_loop()


if __name__ == "__main__":
    main()
