#!/usr/bin/env python3
"""Run Coach: keeps a running plan in the Omarchy bar.

Reads plan.json, the Garmin activity FIT files and (optionally) Strava, judges
each run against its planned session and writes status.json, which the bar
widget (okox.run-coach) renders.

Subcommands:
  scan                 import new FIT files / Strava runs, rewrite status.json
  status               print status.json (rebuilds it first, no network)
  waybar               print {"text","tooltip","class","percentage"}
  detail               print the current session and the last run as text
  mark done|skip|move|clear [SESSION_ID] [--km N]
  init-plan --race-date D  create ~/.config/run-coach/plan.json from the example
  km DATE DELTA        add DELTA km to DATE (late watch start, treadmill...)
  garmin-auth          log in to Garmin Connect (one-time) to download FIT files
  garmin-disconnect    forget the Garmin Connect token
  strava-auth          connect Strava (one-time, opens the browser)
  strava-disconnect    forget the Strava token
"""
import argparse
import datetime as dt
import glob
import http.server
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
APP = Path(os.environ.get("RUN_COACH_HOME", HOME / ".local/share/run-coach"))
STATE = APP / "state.json"
STATUS_DIR = Path(os.environ.get("RUN_COACH_STATE", HOME / ".local/state/run-coach"))
STATUS = STATUS_DIR / "status.json"
CONF_DIR = Path(os.environ.get("RUN_COACH_CONFIG", HOME / ".config/run-coach"))
CONFIG = CONF_DIR / "config.json"
PLAN = CONF_DIR / "plan.json"  # your plan: see PLAN.md
TOKEN = CONF_DIR / "strava.json"
GARMIN_TOKEN = CONF_DIR / "garmin_tokens.json"
GARMIN_DIR = APP / "garmin-fit"

DEFAULTS = {
    # A folder you copy Garmin .fit files into (optional; "" = none).
    "activity_dir": "",
    # IANA name, e.g. "Europe/Madrid"; "" = the system's time zone.
    "timezone": "",
    "strava": True,
    # Coach notes written by Claude Code (`claude -p`), if it's installed.
    "coach_notes": True,
    "note_model": "",
    "claude_bin": "",
    # Optional page the panel's "Full plan" button opens (html/pdf/anything).
    "plan_html": "",
    # The bar text budget.
    "max_text": 45,
}


def system_tz():
    try:
        return str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1]
    except (IndexError, OSError):
        return "UTC"

GLYPH = {
    "planned": "\U000F070E",  # md-run
    "done": "\U000F012C",     # md-check
    "warn": "\U000F002A",     # md-alert-circle
    "missed": "\U000F0156",   # md-close
    "rest": "\U000F04B2",     # md-sleep
    "race": "\U000F023C",     # md-flag-checkered
    "setup": "\U000F070E",
}
FIT_VERSION = 2  # bump to re-decode cached FIT files after a decoder change
RUN_SPORTS = {"running", "Run", "TrailRun", "VirtualRun"}
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_VERSION = 2  # bump to re-fetch run detail after adding fields
LOAD_DAYS = 120     # Strava activities kept for Fitness / Fatigue / Form
STRAVA_PORT = 8723


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, default=str)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def config():
    c = dict(DEFAULTS)
    c.update(load_json(CONFIG, {}))
    return c


def tz():
    return ZoneInfo(config()["timezone"] or system_tz())


def now():
    fake = os.environ.get("RUN_COACH_NOW")  # for testing: 2026-11-20T09:00
    if fake:
        return dt.datetime.fromisoformat(fake).replace(tzinfo=tz())
    return dt.datetime.now(tz())


def d(s):
    return dt.date.fromisoformat(s)


def pace_str(sec_per_km):
    if not sec_per_km or sec_per_km <= 0 or sec_per_km > 1200:
        return "-"
    s = int(round(sec_per_km))
    return f"{s // 60}:{s % 60:02d}"


def pace_sec(txt):
    m, s = txt.split(":")
    return int(m) * 60 + int(s)


def dur_str(sec):
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def dow(date):
    return date.strftime("%a")


def clip(text, n):
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def num(v, nd=1):
    return None if v is None else round(float(v), nd)


# ---------------------------------------------------------------- plan steps

STEP_RE = re.compile(r"^([\d.]+) (km|min|s) (.*?) \((HR (\d+)-(\d+)|(\d+:\d\d)-(\d+:\d\d)/km|by feel)\)$")
REST_WORDS = ("warm", "cool", "jog", "recovery", "walk", "easy", "stride")


def parse_step(txt):
    m = STEP_RE.match(txt.strip())
    if not m:
        return None
    val, unit, name, _, hlo, hhi, p1, p2 = m.groups()
    step = {"name": name, "dur": f"{val} {unit}", "kind": "open"}
    if hlo:
        step.update(kind="hr", lo=int(hlo), hi=int(hhi))
    elif p1:
        a, b = sorted((pace_sec(p1), pace_sec(p2)))
        step.update(kind="pace", lo=a, hi=b)  # lo = fastest, hi = slowest (s/km)
    low = name.lower()
    step["work"] = (step["kind"] == "pace" and not any(w in low for w in ("warm", "cool", "jog", "walk"))) or (
        step["kind"] == "hr" and "benchmark" in low)
    return step


def plan_steps(steps_text):
    """Expand '2 km Warm up (HR 150-170) | 3 x [8 min T (4:50-4:40/km), 2 min Jog (by feel)]'."""
    flat = []
    for item in (steps_text or "").split(" | "):
        item = item.strip()
        rep = re.match(r"^(\d+) x \[(.*)\]$", item)
        if rep:
            inner = [parse_step(x) for x in re.split(r",\s(?=[\d.]+ (?:km|min|s) )", rep.group(2))]
            if None in inner:
                return []
            for _ in range(int(rep.group(1))):
                flat.extend(dict(s) for s in inner)
        else:
            s = parse_step(item)
            if s is None:
                return []
            flat.append(s)
    return flat


# ---------------------------------------------------------------- plan

class Plan:
    def __init__(self, raw, overrides):
        self.raw = raw
        self.goal = raw["goal"]
        self.race_date = d(raw["goal"]["date"])
        self.zones = raw["zones_hr"]
        self.bands = hr_zone_bands(self.zones)
        self.km_target = {int(k): v for k, v in raw["weekly_km_target"].items()}
        self.sessions = []
        bench_dates = set()
        for c in raw.get("checkpoints", []):
            if "benchmark" in c.get("test", "").lower():
                bench_dates.update(c.get("dates", []))
        for s in raw["sessions"]:
            s = dict(s)
            o = overrides.get(s["id"], {})
            s["planned_date"] = s["date"]
            s["date"] = o.get("moved_to", s["date"])
            s["manual"] = o.get("status")  # done | skipped | None
            s["benchmark"] = s["planned_date"] in bench_dates and s["type"] == "test"
            s["flat_steps"] = plan_steps(s.get("steps", "")) if s["type"] not in ("easy", "rest") else []
            self.sessions.append(s)
        self.sessions.sort(key=lambda s: (s["date"], s["id"]))
        self.start = d(min(s["planned_date"] for s in self.sessions))
        self.last_week = max([s.get("week", 0) for s in self.sessions] + list(self.km_target))
        self.short = self.goal.get("short") or self.goal["race"]
        # The aerobic benchmark: a block at a fixed HR whose pace you track.
        b = raw.get("benchmark") or {}
        self.bench = {"hr": b.get("hr", [160, 165]), "km": b.get("km", 5), "start_km": b.get("start_km", 2),
                      "baseline": b.get("baseline") or {}}
        self.week0_monday = self.start - dt.timedelta(days=self.start.weekday())

    def on(self, date):
        return [s for s in self.sessions if s["date"] == date.isoformat()]

    def by_id(self, sid):
        for s in self.sessions:
            if s["id"] == sid:
                return s
        return None

    def week_of(self, date):
        return max(0, min(self.last_week, (date - self.week0_monday).days // 7))

    def cap_for(self, s):
        recovery = "recovery" in s.get("title", "").lower()
        return self.zones["recovery"][1] if recovery else self.zones["easy"][1]


# ---------------------------------------------------------------- activities

def hr_drift(samples, cap, skip=600):
    """samples: [(t_sec, hr)]. Average HR after `skip` s and % of time above cap."""
    pts = [(t, h) for t, h in samples if h and h > 40]
    if len(pts) < 10:
        return None
    total = above = hr_t = 0.0
    for (t0, h0), (t1, _) in zip(pts, pts[1:]):
        if t0 < skip:
            continue
        dt_ = min(max(t1 - t0, 0), 10)
        total += dt_
        hr_t += h0 * dt_
        if h0 > cap:
            above += dt_
    if total < 60:
        return None
    return {"avg_after_10": round(hr_t / total), "pct_above": round(100 * above / total), "cap": cap}


ZONE_ORDER = [("recovery", "Recovery"), ("easy", "Easy"), ("steady", "Steady"),
              ("threshold", "Threshold"), ("tempo_vo2", "Tempo / VO2"), ("interval", "Interval")]


def hr_zone_bands(zones):
    """The plan's HR zones overlap (recovery 150-165, easy 160-170...). For
    time in zone each band runs from just above the previous zone's top to
    its own top: easy = 166-170 would hide most easy running, so the lower
    edge of a zone is the previous zone's top + 1 except for easy, which
    starts at its own floor (160) and pushes recovery down to 159."""
    bands, prev_hi = [], 0
    for i, (key, name) in enumerate(ZONE_ORDER):
        lo, hi = zones[key]
        nxt = ZONE_ORDER[i + 1][0] if i + 1 < len(ZONE_ORDER) else None
        if nxt == "easy":
            hi = zones["easy"][0] - 1
        lo = max(lo, prev_hi + 1) if key != "recovery" else 0
        bands.append({"key": key, "name": name, "lo": lo, "hi": hi if nxt else 999,
                      "label": f"<{hi + 1}" if key == "recovery" else (f"{lo}+" if not nxt else f"{lo}-{hi}")})
        prev_hi = hi
    return bands


def time_in_zones(samples, bands):
    pts = [(t, h) for t, h in samples if h and h > 40]
    if len(pts) < 10:
        return None
    secs = [0.0] * len(bands)
    for (t0, h0), (t1, _) in zip(pts, pts[1:]):
        dt_ = min(max(t1 - t0, 0), 10)
        for i, b in enumerate(bands):
            if h0 <= b["hi"]:
                secs[i] += dt_
                break
    total = sum(secs) or 1
    return [{"key": b["key"], "name": b["name"], "label": b["label"], "secs": round(s), "time": dur_str(s),
             "pct": round(100 * s / total)} for b, s in zip(bands, secs)]


def decode_fit(path):
    from garmin_fit_sdk import Decoder, Stream
    msgs, errors = Decoder(Stream.from_file(str(path))).read(
        convert_datetimes_to_dates=True, expand_sub_fields=True, merge_heart_rates=True)
    ses = (msgs.get("session_mesgs") or [{}])[0]
    sport = str(ses.get("sport", ""))
    start = ses.get("start_time")
    if not start:
        return None
    act = {
        "source": "garmin",
        "file": Path(path).name,
        "sport": "running" if sport == "running" else sport,
        "start_utc": start.astimezone(dt.timezone.utc).isoformat(),
        "distance_m": ses.get("total_distance") or 0,
        "timer_s": ses.get("total_timer_time") or 0,
        "elapsed_s": ses.get("total_elapsed_time") or 0,
        "avg_hr": ses.get("avg_heart_rate"),
        "max_hr": ses.get("max_heart_rate"),
        "cadence_spm": (ses.get("avg_running_cadence") or ses.get("avg_cadence") or 0) * 2
        + 2 * (ses.get("avg_fractional_cadence") or 0) or None,
        "vr_pct": num(ses.get("avg_vertical_ratio"), 2),
        "vo_cm": num((ses.get("avg_vertical_oscillation") or 0) / 10, 1) or None,
        "gct_ms": num(ses.get("avg_stance_time"), 0),
        "step_m": num((ses.get("avg_step_length") or 0) / 1000, 2) or None,
    }
    if act["sport"] != "running":
        return act
    t0 = start.timestamp()
    recs = msgs.get("record_mesgs") or []
    act["hr_samples"] = [(round(r["timestamp"].timestamp() - t0), r.get("heart_rate"))
                         for r in recs if r.get("timestamp") and r.get("heart_rate")]
    dist = [(round(r["timestamp"].timestamp() - t0), r.get("distance"), r.get("heart_rate"), r.get("vertical_ratio"))
            for r in recs if r.get("timestamp") and r.get("distance") is not None]
    act["dist_samples"] = dist[:: max(1, len(dist) // 1500)]
    wsteps = {}
    for st in msgs.get("workout_step_mesgs") or []:
        wsteps[st.get("message_index")] = st
    laps = []
    for lap in msgs.get("lap_mesgs") or []:
        L = {
            "dist": lap.get("total_distance") or 0,
            "time": lap.get("total_timer_time") or 0,
            "hr": lap.get("avg_heart_rate"),
            "vr": num(lap.get("avg_vertical_ratio"), 2),
            "cad": (lap.get("avg_running_cadence") or 0) * 2 or None,
            "intensity": str(lap.get("intensity") or ""),
            "step_idx": lap.get("wkt_step_index"),
        }
        idx = lap.get("wkt_step_index")
        st = wsteps.get(idx) if idx is not None else None
        if st is not None:
            L["step"] = fit_step(st)
        laps.append(L)
    act["laps"] = laps
    wk = msgs.get("workout_mesgs") or []
    name = wk[0].get("wkt_name") if wk else None
    if isinstance(name, (list, tuple)):
        name = next((x for x in name if x), None)
    act["workout"] = name
    return act


def fit_step(st):
    name = st.get("wkt_step_name") or ""
    t = st.get("target_type")
    step = {"name": name, "kind": "open"}
    if t == "heart_rate" and st.get("custom_target_heart_rate_low"):
        step.update(kind="hr", lo=st["custom_target_heart_rate_low"] - 100, hi=st["custom_target_heart_rate_high"] - 100)
    elif t == "speed" and st.get("custom_target_speed_low"):
        a, b = sorted((1000 / st["custom_target_speed_low"], 1000 / st["custom_target_speed_high"]))
        step.update(kind="pace", lo=a, hi=b)
    low = name.lower()
    inten = str(st.get("intensity", ""))
    step["work"] = inten not in ("warmup", "cooldown", "recovery", "rest") and (
        (step["kind"] == "pace" and not any(w in low for w in ("warm", "cool", "jog", "walk")))
        or (step["kind"] == "hr" and "benchmark" in low))
    return step


def activity_dir():
    p = config().get("activity_dir")
    return Path(os.path.expanduser(p)) if p else None


def scan_fit(state, plan_start):
    folder = activity_dir()
    cache = state.setdefault("fit", {})
    cutoff = (plan_start - dt.timedelta(days=plan_start.weekday())).isoformat()  # Monday of week 0
    seen = set()
    files = []
    for base in (folder, GARMIN_DIR):
        if base and base.is_dir():
            files += glob.glob(str(base / "**/*.fit"), recursive=True) + glob.glob(str(base / "**/*.FIT"), recursive=True)
    for f in sorted(files):
        p = Path(f)
        m = re.match(r"(\d{4}-\d\d-\d\d)", p.name)
        if m and m.group(1) < cutoff:
            continue
        st = p.stat()
        key = str(p)
        seen.add(key)
        c = cache.get(key)
        if c and c.get("mtime") == st.st_mtime and c.get("size") == st.st_size and c.get("v") == FIT_VERSION:
            continue
        try:
            act = decode_fit(p)
        except Exception as e:  # a half-copied file: try again next scan
            cache[key] = {"mtime": None, "size": None, "error": str(e)[:200]}
            continue
        cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "v": FIT_VERSION, "activity": act}
    for key in list(cache):
        if key not in seen:
            del cache[key]
    if folder and not folder.is_dir():
        return f"activity folder missing: {folder}"
    return None


# ---------------------------------------------------------------- garmin connect

def garmin_client():
    from garminconnect import Garmin
    g = Garmin()
    g.login(tokenstore=str(GARMIN_TOKEN))
    os.chmod(GARMIN_TOKEN, 0o600)
    return g


def scan_garmin(state, plan_start, force=False):
    """Download the original FIT of each new run from Garmin Connect into
    GARMIN_DIR (never into the user's activity folder)."""
    if not GARMIN_TOKEN.exists():
        return None
    g = state.setdefault("garmin", {"downloaded": {}})
    last = g.get("last_try")
    if not force and last and time.time() - last < 180:
        return None if not g.get("error") else "Garmin Connect: " + g["error"]
    g["last_try"] = time.time()
    try:
        import io
        import zipfile
        api = garmin_client()
        start = (plan_start - dt.timedelta(days=plan_start.weekday())).isoformat()
        acts = api.get_activities_by_date(start, now().date().isoformat())
        GARMIN_DIR.mkdir(parents=True, exist_ok=True)
        done = g.setdefault("downloaded", {})
        for a in acts:
            aid = str(a.get("activityId"))
            kind = (a.get("activityType") or {}).get("typeKey", "")
            if "running" not in kind:
                continue
            if aid in done and (GARMIN_DIR / done[aid]).exists():
                continue
            raw = api.download_activity(aid, dl_fmt=api.ActivityDownloadFormat.ORIGINAL)
            if len(raw) > 50_000_000:
                continue
            stamp = re.sub(r"[^0-9]", "-", (a.get("startTimeLocal") or "")[:19]) or aid
            name = f"{stamp}_{aid}.fit"
            if raw[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    fits = [n for n in z.namelist() if n.lower().endswith(".fit")]
                    if not fits:
                        continue
                    raw = z.read(fits[0])
            (GARMIN_DIR / name).write_bytes(raw)
            done[aid] = name
        g["last_sync"] = now().isoformat()
        g.pop("error", None)
        fetch_fitness(state, api, plan_start)
        return None
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"[:200]
        return "Garmin Connect: " + g["error"]


def fetch_fitness(state, api, plan_start):
    """VO2 max, race predictions, readiness, load: Garmin's own numbers,
    fetched at most every 6 h. Each piece is optional."""
    f = state.setdefault("fitness", {})
    if f.get("fetched") and time.time() - f["fetched"] < 6 * 3600:
        return
    today = now().date().isoformat()
    latest = {}

    def get(fn, *a):
        try:
            return fn(*a)
        except Exception:
            return None

    ts = get(api.get_training_status, today) or {}
    vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic") or {})
    if vo2.get("vo2MaxPreciseValue"):
        latest["vo2max"] = vo2["vo2MaxPreciseValue"]
        latest["vo2max_date"] = vo2.get("calendarDate")
    tsd = next(iter(((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}).values()), {})
    if tsd:
        latest["status"] = (tsd.get("trainingStatusFeedbackPhrase") or "").split("_")[0].replace("NO", "No status").title()
        acute = tsd.get("acuteTrainingLoadDTO") or {}
        latest["load_acute"] = acute.get("dailyTrainingLoadAcute")
        latest["load_chronic"] = acute.get("dailyTrainingLoadChronic")
        latest["acwr"] = acute.get("dailyAcuteChronicWorkloadRatio")
        latest["acwr_status"] = (acute.get("acwrStatus") or "").title()
    lb = next(iter(((ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}).values()), {})
    if lb:
        latest["balance"] = {
            "phrase": (lb.get("trainingBalanceFeedbackPhrase") or "").replace("_", " ").capitalize(),
            "low": [round(lb.get("monthlyLoadAerobicLow") or 0), lb.get("monthlyLoadAerobicLowTargetMin"), lb.get("monthlyLoadAerobicLowTargetMax")],
            "high": [round(lb.get("monthlyLoadAerobicHigh") or 0), lb.get("monthlyLoadAerobicHighTargetMin"), lb.get("monthlyLoadAerobicHighTargetMax")],
            "anaerobic": [round(lb.get("monthlyLoadAnaerobic") or 0), lb.get("monthlyLoadAnaerobicTargetMin"), lb.get("monthlyLoadAnaerobicTargetMax")],
        }
    tr = get(api.get_training_readiness, today)
    if tr:
        r0 = tr[0]
        latest["readiness"] = {"score": r0.get("score"), "level": (r0.get("level") or "").title(),
                               "sleep": r0.get("sleepScore"), "recovery_h": round((r0.get("recoveryTime") or 0) / 60, 1),
                               "hrv_week": r0.get("hrvWeeklyAverage")}
    hrv = (get(api.get_hrv_data, today) or {}).get("hrvSummary") or {}
    if hrv:
        latest["hrv"] = {"night": hrv.get("lastNightAvg"), "week": hrv.get("weeklyAvg"), "status": (hrv.get("status") or "").title()}
    es = get(api.get_endurance_score, today) or {}
    if es.get("overallScore"):
        lims = [("Elite", "classificationLowerLimitElite"), ("Superior", "classificationLowerLimitSuperior"),
                ("Expert", "classificationLowerLimitExpert"), ("Well-trained", "classificationLowerLimitWellTrained"),
                ("Trained", "classificationLowerLimitTrained"), ("Intermediate", "classificationLowerLimitIntermediate")]
        cls = next((n for n, k in lims if es.get(k) and es["overallScore"] >= es[k]), "Recreational")
        nxt = next((es[k] for n, k in reversed(lims) if es.get(k) and es["overallScore"] < es[k]), None)
        latest["endurance"] = {"score": es["overallScore"], "class": cls, "next": nxt}
    fa = get(api.get_fitnessage_data, today) or {}
    if fa.get("fitnessAge"):
        latest["fitness_age"] = round(fa["fitnessAge"], 1)
        latest["age"] = fa.get("chronologicalAge")
    rhr = get(api.get_rhr_day, today) or {}
    v = (((rhr.get("allMetrics") or {}).get("metricsMap") or {}).get("WELLNESS_RESTING_HEART_RATE") or [{}])[0].get("value")
    if v:
        latest["rhr"] = round(v)
    lt = (get(api.get_lactate_threshold) or {}).get("speed_and_heart_rate") or {}
    if lt.get("heartRate"):
        latest["lt_hr"] = lt["heartRate"]
        if lt.get("speed"):  # Garmin stores it in units of 10 m/s
            latest["lt_pace"] = pace_str(1000 / (lt["speed"] * 10))
    # Daily race predictions, from 90 days before the plan.
    start = (plan_start - dt.timedelta(days=90)).isoformat()
    preds = get(api.get_race_predictions, start, today, "daily") or []
    hist = f.setdefault("predictions", {})
    for p in preds if isinstance(preds, list) else []:
        if p.get("calendarDate") and p.get("timeMarathon"):
            hist[p["calendarDate"]] = {"m": p["timeMarathon"], "h": p.get("timeHalfMarathon"),
                                       "k10": p.get("time10K"), "k5": p.get("time5K")}
    if hist:
        last = hist[max(hist)]
        latest["predictions"] = {"date": max(hist), "marathon": last["m"], "half": last["h"], "k10": last["k10"], "k5": last["k5"]}
    if "vo2max" in latest:
        f.setdefault("vo2max", {})[latest.get("vo2max_date") or today] = latest["vo2max"]
    f["latest"] = latest
    f["fetched"] = time.time()


# ---------------------------------------------------------------- strava

def strava_token():
    tok = load_json(TOKEN, None)
    if not tok or not tok.get("refresh_token"):
        return None
    if tok.get("expires_at", 0) < time.time() + 120:
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"], "client_secret": tok["client_secret"],
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}).encode()
        with urllib.request.urlopen("https://www.strava.com/oauth/token", data, timeout=15) as r:
            new = json.load(r)
        tok.update(access_token=new["access_token"], refresh_token=new["refresh_token"], expires_at=new["expires_at"])
        save_json(TOKEN, tok, 0o600)
    return tok["access_token"]


def strava_get(token, path, **params):
    url = f"{STRAVA_API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if int(r.headers.get("Content-Length") or 0) > 20_000_000:
            raise ValueError("response too large")
        return json.load(r)


def scan_strava(state, plan_start):
    if not config().get("strava") or not TOKEN.exists():
        return None
    s = state.setdefault("strava", {"activities": {}})
    try:
        token = strava_token()
        week0 = plan_start - dt.timedelta(days=plan_start.weekday())
        today = now().date()
        after = dt.datetime.combine(min(week0, today - dt.timedelta(days=LOAD_DAYS)), dt.time(), tz()).timestamp()
        detail_from = min(week0, today - dt.timedelta(days=42)).isoformat()
        page, listing = 1, []
        while page <= 5:
            batch = strava_get(token, "/athlete/activities", after=int(after), per_page=100, page=page)
            listing += batch
            if len(batch) < 100:
                break
            page += 1
        acts = s["activities"]
        load = s.setdefault("load", {})
        live = set()
        for a in listing:
            aid = str(a["id"])
            live.add(aid)
            sport = a.get("sport_type", a.get("type"))
            load[aid] = {"date": (a.get("start_date_local") or a["start_date"])[:10], "sport": sport,
                         "re": a.get("suffer_score"), "name": a.get("name"), "km": round((a.get("distance") or 0) / 1000, 2)}
            if sport not in RUN_SPORTS or (a.get("start_date_local") or a["start_date"])[:10] < detail_from:
                acts.pop(aid, None)
                continue
            old = acts.get(aid)
            if old and old.get("detail") == STRAVA_VERSION and old.get("moving_s") == a.get("moving_time"):
                continue
            act = {
                "source": "strava", "id": aid, "name": a.get("name"), "sport": "running",
                "start_utc": a["start_date"].replace("Z", "+00:00"),
                "distance_m": a.get("distance") or 0, "timer_s": a.get("moving_time") or 0,
                "moving_s": a.get("moving_time"), "elapsed_s": a.get("elapsed_time") or 0,
                "avg_hr": a.get("average_heartrate"), "max_hr": a.get("max_heartrate"),
                "cadence_spm": (a.get("average_cadence") or 0) * 2 or None,
            }
            det = strava_get(token, f"/activities/{aid}")
            act.update(description=det.get("description") or "", suffer_score=det.get("suffer_score"),
                       kudos=det.get("kudos_count"), gear=(det.get("gear") or {}).get("name"),
                       best_efforts=[[b.get("name"), b.get("elapsed_time"), b.get("distance")] for b in det.get("best_efforts") or []])
            act["laps"] = [{"dist": L.get("distance") or 0, "time": L.get("moving_time") or 0,
                            "hr": L.get("average_heartrate"), "cad": (L.get("average_cadence") or 0) * 2 or None}
                           for L in det.get("laps") or []]
            if a.get("has_heartrate"):
                st = strava_get(token, f"/activities/{aid}/streams", keys="time,heartrate,distance", key_by_type="true")
                t = st.get("time", {}).get("data", [])
                h = st.get("heartrate", {}).get("data", [])
                dd = st.get("distance", {}).get("data", [])
                act["hr_samples"] = list(zip(t, h))
                step = max(1, len(t) // 1500)
                act["dist_samples"] = [(t[i], dd[i], h[i] if i < len(h) else None, None) for i in range(0, min(len(t), len(dd)), step)]
            act["detail"] = STRAVA_VERSION
            acts[aid] = act
        for d_ in (acts, load):
            for aid in list(d_):
                if aid not in live:
                    del d_[aid]  # deleted on Strava, or out of the window
        s["last_sync"] = now().isoformat()
        s.pop("error", None)
        return None
    except Exception as e:
        s["error"] = f"{type(e).__name__}: {e}"[:200]
        return "Strava: " + s["error"]


def merged_runs(state):
    """Garmin FIT and Strava copies of the same run merged; FIT wins for detail."""
    fits = [c["activity"] for c in state.get("fit", {}).values() if c.get("activity")]
    stravas = list(state.get("strava", {}).get("activities", {}).values())
    runs = []
    used = set()
    starts = set()
    for f in sorted(fits, key=lambda x: x.get("file", "")):
        if f.get("sport") != "running":
            continue
        t = dt.datetime.fromisoformat(f["start_utc"]).timestamp()
        if any(abs(t - x) < 180 for x in starts):
            continue  # same run already read from the other folder
        starts.add(t)
        twin = None
        for s in stravas:
            if s.get("sport") == "running" and s.get("id") not in used and abs(dt.datetime.fromisoformat(s["start_utc"]).timestamp() - t) < 180:
                twin = s
                break
        r = dict(f)
        if twin:
            used.add(twin["id"])
            r["source"] = "garmin+strava"
            r["strava_id"] = twin["id"]
            r["name"] = twin.get("name")
            for k in ("description", "suffer_score", "kudos", "gear", "best_efforts"):
                if twin.get(k) is not None:
                    r[k] = twin[k]
        runs.append(r)
    for s in stravas:
        if s.get("sport") == "running" and s.get("id") not in used and s.get("start_utc"):
            runs.append(dict(s))
    zone = tz()
    for r in runs:
        local = dt.datetime.fromisoformat(r["start_utc"]).astimezone(zone)
        r["date"] = local.date().isoformat()
        r["time_local"] = local.strftime("%H:%M")
        r["pace"] = r["timer_s"] / (r["distance_m"] / 1000) if r["distance_m"] > 100 else None
    runs.sort(key=lambda r: r["start_utc"])
    return runs


# ---------------------------------------------------------------- analysis

STAGE_NAMES = {"warmup": "Warm-up", "active": "Run", "interval": "Work", "recovery": "Recovery",
               "cooldown": "Cool-down", "rest": "Rest"}


def run_chart(run, max_points=240):
    """Pace and HR along the distance, plus the stages (laps merged by
    workout step) to shade behind them."""
    pts = [(t, dd, h) for t, dd, h, *_ in run.get("dist_samples") or [] if dd is not None]
    if len(pts) < 20:
        return None
    out = []
    j = k = 0
    for i, (t, dd, h) in enumerate(pts):
        # speed over a ~30 s window centred on the sample
        while j < i and t - pts[j][0] > 15:
            j += 1
        k = max(k, i)
        while k < len(pts) - 1 and pts[k][0] - t < 15:
            k += 1
        dt_ = pts[k][0] - pts[j][0]
        dd_ = pts[k][1] - pts[j][1]
        pace = dt_ / (dd_ / 1000) if dd_ > 5 and dt_ > 0 else None
        if pace and pace > 900:  # walking or stopped: leave a gap
            pace = None
        out.append((dd / 1000, pace, h))
    step = max(1, len(out) // max_points)
    out = out[::step]
    stages, at = [], 0.0
    for n, L in enumerate(run.get("laps") or [], 1):
        km = (L.get("dist") or 0) / 1000
        if km <= 0:
            continue
        kind = L.get("intensity") or "active"
        name = (L.get("step") or {}).get("name") or STAGE_NAMES.get(kind, kind.title())
        key = (kind, L.get("step_idx"), name)
        if stages and stages[-1]["_key"] == key and kind in ("warmup", "active", "cooldown"):
            stages[-1]["to"] = round(at + km, 3)
            stages[-1]["laps"].append(n)
        else:
            stages.append({"_key": key, "kind": kind, "name": name, "from": round(at, 3), "to": round(at + km, 3), "laps": [n],
                           "_t": 0.0, "_d": 0.0, "_h": 0.0, "_ht": 0.0})
        g_ = stages[-1]
        g_["_t"] += L.get("time") or 0
        g_["_d"] += L.get("dist") or 0
        if L.get("hr"):
            g_["_h"] += L["hr"] * (L.get("time") or 0)
            g_["_ht"] += L.get("time") or 0
        at += km
    for st in stages:
        del st["_key"]
        t_, d_, h_, ht_ = st.pop("_t"), st.pop("_d"), st.pop("_h"), st.pop("_ht")
        st["pace"] = pace_str(t_ / (d_ / 1000)) if d_ > 50 else "-"
        st["hr"] = round(h_ / ht_) if ht_ else None
        st["time"] = dur_str(t_)
        lap_ids = st.pop("laps")
        st["lap"] = f"lap {lap_ids[0]}" if len(lap_ids) == 1 else f"laps {lap_ids[0]}-{lap_ids[-1]}"
    return {"points": [[round(a, 3), round(b) if b else None, c] for a, b, c in out], "stages": stages}


def judge_laps(laps, steps):
    """Pair laps with workout steps. Returns per-lap verdicts for work steps."""
    if not laps:
        return None
    if all("step" in L for L in laps):
        pairs = [(L, L["step"]) for L in laps]
    elif steps and len(laps) in (len(steps), len(steps) + 1):
        pairs = list(zip(laps, steps))
    else:
        return {"matched": False, "laps": len(laps), "steps": len(steps)}
    reps = []
    for L, st in pairs:
        if not st.get("work") or L["dist"] < 50:
            continue
        pace = L["time"] / (L["dist"] / 1000)
        if st["kind"] == "pace":
            ok = st["lo"] - 5 <= pace <= st["hi"] + 5
            tgt = f"{pace_str(st['lo'])}-{pace_str(st['hi'])}"
        else:
            ok = L.get("hr") is not None and st["lo"] - 2 <= L["hr"] <= st["hi"] + 2
            tgt = f"HR {st['lo']}-{st['hi']}"
        reps.append({"name": st["name"], "pace": pace_str(pace), "hr": L.get("hr"), "target": tgt, "ok": ok})
    return {"matched": True, "reps": reps, "on_target": sum(r["ok"] for r in reps), "total": len(reps)}


def benchmark_metrics(run, laps_result, bench):
    """The benchmark block (e.g. 5 km at HR 160-165): its pace + vertical ratio."""
    km, start = bench["km"] * 1000, bench["start_km"] * 1000
    # Prefer the lap the watch made for the benchmark step.
    for L in run.get("laps") or []:
        st = L.get("step") or {}
        if "benchmark" in st.get("name", "").lower() and L["dist"] > 0.6 * km:
            return {"pace": L["time"] / (L["dist"] / 1000), "hr": L.get("hr"), "vr": L.get("vr"), "how": "lap"}
    laps = run.get("laps") or []
    if laps_result and laps_result.get("matched") and len(laps) >= 2 and 0.9 * km < laps[1]["dist"] < 1.1 * km:
        L = laps[1]
        return {"pace": L["time"] / (L["dist"] / 1000), "hr": L.get("hr"), "vr": L.get("vr"), "how": "lap"}
    # Fallback: that stretch of distance from the samples.
    pts = run.get("dist_samples") or []
    seg = [p for p in pts if p[1] is not None and start <= p[1] <= start + km]
    if len(seg) > 10 and seg[-1][1] - seg[0][1] > 0.8 * km:
        t = seg[-1][0] - seg[0][0]
        got_km = (seg[-1][1] - seg[0][1]) / 1000
        hrs = [p[2] for p in seg if p[2]]
        vrs = [p[3] for p in seg if p[3]]
        return {"pace": t / got_km, "hr": round(sum(hrs) / len(hrs)) if hrs else None,
                "vr": round(sum(vrs) / len(vrs), 2) if vrs else None, "how": f"km {start / 1000:g}-{(start + km) / 1000:g}"}
    return None


def analyse(session, runs, plan):
    """Combine the day's runs and judge them against the session."""
    total_m = sum(r["distance_m"] for r in runs)
    total_s = sum(r["timer_s"] for r in runs)
    main = max(runs, key=lambda r: r["distance_m"])
    hr_w = sum((r.get("avg_hr") or 0) * r["timer_s"] for r in runs if r.get("avg_hr"))
    hr_t = sum(r["timer_s"] for r in runs if r.get("avg_hr"))
    out = {
        "date": main["date"], "time": main["time_local"], "runs": len(runs),
        "distance_km": round(total_m / 1000, 2), "duration": dur_str(total_s),
        "pace": pace_str(total_s / (total_m / 1000)) if total_m > 100 else "-",
        "avg_hr": round(hr_w / hr_t) if hr_t else None, "max_hr": max((r.get("max_hr") or 0) for r in runs) or None,
        "cadence": num(main.get("cadence_spm"), 0), "vr": main.get("vr_pct"), "vo_cm": main.get("vo_cm"),
        "gct": main.get("gct_ms"), "step_m": main.get("step_m"),
        "source": " + ".join(sorted({r["source"] for r in runs})), "name": main.get("name"),
        "workout": main.get("workout"), "verdict": "", "status": "done", "notes": [],
    }
    if main.get("strava_id") or main.get("source") == "strava":
        out["strava"] = {"id": main.get("strava_id") or main.get("id"), "name": main.get("name"),
                         "description": main.get("description") or "", "relative_effort": main.get("suffer_score"),
                         "kudos": main.get("kudos"), "gear": main.get("gear")}
    # Time in zone over every run of the day (the jump back to t=0 between
    # runs counts as zero seconds).
    zones = time_in_zones([p for r in runs for p in r.get("hr_samples") or []], plan.bands)
    chart = run_chart(main)
    if chart:
        out["chart"] = chart
    if zones:
        out["zones"] = zones
    out["splits"] = [{"n": i + 1, "km": round(L["dist"] / 1000, 2), "time": dur_str(L["time"]),
                      "pace": pace_str(L["time"] / (L["dist"] / 1000)) if L["dist"] > 50 else "-",
                      "hr": L.get("hr"), "step": (L.get("step") or {}).get("name")}
                     for i, L in enumerate(main.get("laps") or []) if L.get("dist", 0) > 5]
    if not session:  # unplanned: hold it to the easy cap
        session = {"type": "easy", "title": "Unplanned", "steps": "", "flat_steps": []}
    kind = session["type"]
    steps = session.get("flat_steps") or []
    has_work = any(s.get("work") for s in steps) or any((L.get("step") or {}).get("work") for L in main.get("laps") or [])
    cap = plan.cap_for(session)
    drift = hr_drift(main.get("hr_samples") or [], cap) if main.get("hr_samples") else None
    if drift:
        out["hr_after_10"] = drift["avg_after_10"]
        out["pct_above_cap"] = drift["pct_above"]
        out["cap"] = cap
    if kind in ("easy", "long", "rest") or (kind == "key" and not has_work):
        if drift and (drift["avg_after_10"] > cap or drift["pct_above"] > 20):
            out["status"] = "warn"
            out["verdict"] = f"Easy too hard (HR {drift['avg_after_10']})" if drift["avg_after_10"] > cap \
                else f"{drift['pct_above']}% above {cap} bpm"
        elif drift:
            out["verdict"] = f"Easy, HR {drift['avg_after_10']} (cap {cap})"
        else:
            out["verdict"] = "Done (no HR data)"
            if out["avg_hr"] and out["avg_hr"] > cap:
                out["status"] = "warn"
                out["verdict"] = f"Easy too hard (HR {out['avg_hr']})"
        if kind == "rest":
            out["verdict"] = "Ran on a rest day · " + out["verdict"]
        elif session.get("title") == "Unplanned":
            out["verdict"] = "Unplanned · " + out["verdict"]
        return out
    lr = judge_laps(main.get("laps") or [], steps)
    if lr and lr.get("matched") and lr["total"]:
        out["reps"] = lr["reps"]
        out["verdict"] = f"{lr['on_target']} of {lr['total']} reps on target"
        if lr["on_target"] < math.ceil(lr["total"] * 2 / 3):
            out["status"] = "warn"
    elif lr and not lr.get("matched"):
        out["verdict"] = f"Laps don't match the workout ({lr['laps']} laps, {lr['steps']} steps)"
    else:
        out["verdict"] = "Done"
    if session.get("benchmark"):
        b = benchmark_metrics(main, lr, plan.bench)
        if b:
            out["benchmark"] = {"pace": pace_str(b["pace"]), "pace_s": round(b["pace"]), "hr": b["hr"], "vr": b["vr"], "how": b["how"]}
            out["verdict"] = f"Benchmark {pace_str(b['pace'])}/km at HR {b['hr']}" + (f" · VR {b['vr']}%" if b.get("vr") else "")
            lo_, hi_ = plan.bench["hr"]
            if b["hr"] and b["hr"] > hi_ + 2:
                out["status"] = "warn"
                out["notes"].append(f"HR above the {lo_}-{hi_} band, so the pace isn't comparable")
    return out


# ---------------------------------------------------------------- status

def sources_fresh_for(date, state, runs):
    """Can we say a session on `date` was missed? Only if some source has
    data from after it: Strava synced since, or a later Garmin file."""
    for src in ("strava", "garmin"):
        s = state.get(src, {})
        if s.get("last_sync") and not s.get("error"):
            if dt.datetime.fromisoformat(s["last_sync"]).date() > date:
                return True
    return any(r["date"] > date.isoformat() and "garmin" in r["source"] for r in runs)


def build_status(state, plan, problems):
    n = now()
    today = n.date()
    runs = [r for r in merged_runs(state) if r["date"] <= today.isoformat()]
    by_date = {}
    for r in runs:
        by_date.setdefault(r["date"], []).append(r)
    km_adj = state.get("km_adjust", {})

    # Judge every session up to today.
    results = {}
    for s in plan.sessions:
        sd = d(s["date"])
        day_runs = by_date.get(s["date"], [])
        # A real run wins over a Skip pressed before it synced.
        if day_runs and s["type"] != "rest":
            a = analyse(s, day_runs, plan)
            results[s["id"]] = a
            s["status"] = a["status"]
        elif s["manual"] == "skipped":
            s["status"] = "skipped"
        elif s["manual"] == "done":
            s["status"] = "done"
        elif s["type"] == "rest":
            s["status"] = "rest"
        elif sd < today and n >= dt.datetime.combine(sd + dt.timedelta(days=1), dt.time(12), n.tzinfo):
            s["status"] = "missed" if sources_fresh_for(sd, state, runs) else "unconfirmed"
        else:
            s["status"] = "planned"

    # Weeks, Monday to Sunday. Runs not on a session day still count for km.
    mon = today - dt.timedelta(days=today.weekday())

    def week_days(monday):
        days = []
        for i in range(7):
            day = monday + dt.timedelta(days=i)
            iso = day.isoformat()
            ss = plan.on(day)
            day_runs = by_date.get(iso, [])
            km = sum(r["distance_m"] for r in day_runs) / 1000 + km_adj.get(iso, 0)
            main = next((s for s in ss if s["type"] != "rest"), ss[0] if ss else None)
            run = None
            if day_runs:
                run = results.get(main["id"]) if main and main["id"] in results else analyse(main, day_runs, plan)
                run = dict(run, note=(state.get("notes") or {}).get(iso, {}).get("text"), session=f"{main['id']} {main['title']}" if main and main["type"] != "rest" else
                           ("Rest day" if main else "Unplanned"))
            if run and not main and day < plan.start:  # nothing to judge it against yet
                run["verdict"] = "Before the plan started" + (f" · avg HR {run['avg_hr']}" if run.get("avg_hr") else "")
                run["status"] = "done"
                run["session"] = "Before the plan"
            status = main["status"] if main else "none"
            if run and (not main or main["type"] == "rest"):
                status = "extra"  # a run nobody planned
            elif km and status == "none":
                status = "done"
            days.append({
                "date": iso, "dow": dow(day), "day": day.day, "today": day == today, "future": day > today,
                "id": main["id"] if main else "",
                "title": main["title"] if main else (("Before plan" if day < plan.start else "Extra run") if run else ""),
                "type": main["type"] if main else "none", "status": status, "km": round(km, 1),
                "steps": [x.strip() for x in (main.get("steps") or "").split(" | ") if x.strip()] if main else [],
                "garmin": main.get("garmin_workout") if main else None,
                "verdict": run["verdict"] if run else "", "run": run,
            })
        return days

    def week_summary(monday):
        sunday = monday + dt.timedelta(days=6)
        n_ = plan.week_of(monday)
        km_ = sum(r["distance_m"] for r in runs if monday.isoformat() <= r["date"] <= sunday.isoformat()) / 1000
        km_ += sum(v for k, v in km_adj.items() if monday.isoformat() <= k <= sunday.isoformat())
        tgt = plan.km_target.get(n_, 0)
        pl = [s for s in plan.sessions if monday.isoformat() <= s["date"] <= sunday.isoformat() and s["type"] != "rest"]
        dn = sum(1 for s in pl if s["status"] in ("done", "warn"))
        phase = next((s["phase"] for s in pl), "")
        return {"number": n_, "phase": phase, "km": round(km_, 1), "target": tgt,
                "pct": round(min(1.0, km_ / tgt), 3) if tgt else 0, "done": dn, "planned": len(pl),
                "days": week_days(monday), "monday": monday.isoformat(), "current": monday == mon}

    weeks = []
    m_ = min(plan.week0_monday, mon)
    while m_ <= mon:
        weeks.append(week_summary(m_))
        m_ += dt.timedelta(days=7)
    this_week = weeks[-1]

    # Current session: today's, else the next one.
    todays = [s for s in plan.on(today) if s["status"] != "skipped"]
    today_s = next((s for s in todays if s["type"] != "rest"), todays[0] if todays else None)
    upcoming = [s for s in plan.sessions if s["date"] > today.isoformat() and s["type"] != "rest" and s["status"] != "skipped"]
    nxt = upcoming[0] if upcoming else None

    def short(s):
        return f"{dow(d(s['date']))} {s['title']}"

    # Bar state.
    cls, text = "planned", ""
    recent_missed = [s for s in plan.sessions if s["status"] == "missed" and 0 < (today - d(s["date"])).days <= 2]
    if today == plan.race_date:
        cls, text = "race", f"Race day · {plan.goal['target_time']} · {plan.goal.get('goal_pace_watch') or plan.goal.get('goal_pace', '')}"
    elif today_s and today_s["status"] in ("done", "warn"):
        a = results.get(today_s["id"])
        nx = f" · Next: {short(nxt)}" if nxt else ""
        if today_s["status"] == "warn" and a:
            cls, text = "warn", a["verdict"] + nx
        else:
            cls, text = "done", (f"{a['distance_km']:.1f} km" if a else "Done") + nx
    elif today_s and today_s["type"] == "rest":
        if recent_missed:
            cls, text = "missed", f"Missed {short(recent_missed[-1])}" + (f" · Next: {short(nxt)}" if nxt else "")
        else:
            cls, text = "rest", "Rest" + (f" · Next: {short(nxt)}" if nxt else "")
    elif today_s:
        cls, text = "planned", f"{dow(today)} · {today_s['id']} {today_s['title']}"
    elif recent_missed:
        cls, text = "missed", f"Missed {short(recent_missed[-1])}" + (f" · Next: {short(nxt)}" if nxt else "")
    elif nxt:
        cls, text = "planned", f"Next: {short(nxt)}"
    elif today > plan.race_date:
        cls, text = "done", f"{plan.short} done"
    if problems and not text:
        cls, text = "setup", "run-coach: check setup"

    current = today_s if (today_s and today_s["type"] != "rest") else nxt
    last_run = None
    if runs:
        lr = runs[-1]
        sess = next((s for s in plan.sessions if s["date"] == lr["date"] and s["type"] != "rest"), None)
        last_run = results.get(sess["id"]) if sess and sess["id"] in results else analyse(sess, by_date[lr["date"]], plan)
        if last_run is not None:
            last_run = dict(last_run, note=(state.get("notes") or {}).get(lr["date"], {}).get("text"))
            last_run["session"] = f"{sess['id']} {sess['title']}" if sess else "Unplanned"

    # Benchmarks.
    base = plan.bench["baseline"]
    base_pace = None
    if base.get("pace"):
        p = re.findall(r"\d+:\d\d", base["pace"])
        base_pace = sum(map(pace_sec, p)) / len(p) if p else None
    benches = []
    for s in plan.sessions:
        if s["benchmark"]:
            r = results.get(s["id"], {}).get("benchmark")
            benches.append({"id": s["id"], "date": s["date"], "pace": r["pace"] if r else None,
                            "hr": r["hr"] if r else None, "vr": r["vr"] if r else None, "pace_s": r["pace_s"] if r else None})
    first = next((b for b in benches if b["pace_s"]), None)
    ref_pace = first["pace_s"] if first else base_pace
    for b in benches:
        if b["pace_s"] and ref_pace:
            b["delta_s"] = round(b["pace_s"] - ref_pace)
        if b["vr"] and base.get("vertical_ratio_pct"):
            b["delta_vr"] = round(b["vr"] - base["vertical_ratio_pct"], 2)

    # Checkpoints.
    cps = []
    for c in plan.raw.get("checkpoints", []):
        for cd in c.get("dates", [c["date"]] if c.get("date") else []):
            cps.append({"date": cd, "test": c["test"], "target": c["on_track"]})
    cps.sort(key=lambda c: c["date"])
    next_cp = next((c for c in cps if c["date"] >= today.isoformat()), None)
    if next_cp:
        next_cp["days"] = (d(next_cp["date"]) - today).days

    # Upcoming list (next 7 non-rest sessions).
    def sess_out(s):
        if not s:
            return None
        return {"id": s["id"], "date": s["date"], "dow": dow(d(s["date"])), "title": s["title"], "type": s["type"],
                "status": s["status"], "phase": s["phase"], "week": s["week"], "garmin": s.get("garmin_workout"),
                "steps": [x.strip() for x in (s.get("steps") or "").split(" | ") if x.strip()],
                "moved": s["date"] != s["planned_date"], "manual": s["manual"], "benchmark": s["benchmark"],
                "result": results.get(s["id"])}

    sstrava = state.get("strava", {})
    fit_files = [c["activity"] for c in state.get("fit", {}).values() if c.get("activity")]
    last_fit = max((a["start_utc"] for a in fit_files), default=None)
    tooltip_bits = []
    if current:
        tooltip_bits.append(f"{current['id']} {current['title']}")
    w = this_week
    tooltip_bits.append(f"Week {w['number']}: {w['km']:.1f}/{w['target']} km · {w['done']}/{w['planned']} sessions")
    tooltip_bits.append(f"{(plan.race_date - today).days} days to {plan.short}")
    status = {
        "version": 1,
        "generated": n.isoformat(timespec="seconds"),
        "bar": {"text": clip(text, config()["max_text"]), "class": cls, "glyph": GLYPH.get(cls, GLYPH["planned"]),
                "tooltip": "\n".join(tooltip_bits)},
        "today": today.isoformat(),
        "current": sess_out(current),
        "current_is_today": bool(today_s and current is today_s),
        "today_rest": bool(today_s and today_s["type"] == "rest"),
        "next": sess_out(nxt),
        "upcoming": [sess_out(s) for s in upcoming[:6]],
        "week": dict(this_week, phase=this_week["phase"] or (current or {}).get("phase", "")),
        "weeks": weeks,
        # Whole plan, for the weekly-volume chart: target for every week,
        # actual km for the weeks so far.
        "volume": [{"week": n_, "target": t_, "km": next((w["km"] for w in weeks if w["number"] == n_), None),
                    "monday": (plan.week0_monday + dt.timedelta(days=7 * n_)).isoformat(),
                    "phase": next((x["phase"] for x in plan.sessions if x["week"] == n_), "")}
                   for n_, t_ in sorted(plan.km_target.items())],
        "fitness": fitness_out(state, plan),
        "strava": {"connected": TOKEN.exists(), "fitness": strava_fitness(state),
                   "predictions": strava_predictions(runs), "goal_s": fitness_goal_s(plan),
                   "notes_error": state.get("notes_error")} if TOKEN.exists() else None,
        "hr_zones": [{k: b[k] for k in ("key", "name", "label")} for b in plan.bands],
        "last_run": last_run,
        "benchmarks": benches,
        "baseline": {"pace": base.get("pace"), "vr": base.get("vertical_ratio_pct"), "cadence": base.get("cadence_spm"),
                     "gct": base.get("gct_ms"), "vo_cm": base.get("vertical_oscillation_cm")},
        "race": {"name": plan.goal["race"], "short": plan.short, "date": plan.goal["date"], "days": (plan.race_date - today).days,
                 "target": plan.goal["target_time"], "pace": plan.goal.get("goal_pace_watch") or plan.goal.get("goal_pace", "")},
        "next_checkpoint": next_cp,
        "sources": {
            "garmin_connected": GARMIN_TOKEN.exists(),
            "garmin_last_sync": state.get("garmin", {}).get("last_sync"),
            "garmin_error": state.get("garmin", {}).get("error"),
            "strava_connected": TOKEN.exists(),
            "strava_last_sync": sstrava.get("last_sync"),
            "strava_error": sstrava.get("error"),
            "garmin_last_activity": last_fit,
            "activity_dir": config().get("activity_dir") or None,
        },
        "plan_html": plan_html(),
        "rest_note": plan.raw.get("rest_day_note") or "Rest day",
        "problems": problems,
    }
    return status


def fitness_goal_s(plan):
    g = plan.goal["target_time"].split(":")
    return int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2])


def fitness_out(state, plan):
    f = state.get("fitness") or {}
    if not f.get("latest"):
        return None
    goal = plan.goal["target_time"].split(":")
    goal_s = int(goal[0]) * 3600 + int(goal[1]) * 60 + int(goal[2])
    hist = f.get("predictions") or {}
    return dict(f["latest"], goal_s=goal_s, race_date=plan.goal["date"], plan_start=plan.start.isoformat(),
                trend=[[k, v["m"]] for k, v in sorted(hist.items())],
                fetched=dt.datetime.fromtimestamp(f["fetched"], tz()).isoformat(timespec="minutes") if f.get("fetched") else None)


def strava_fitness(state):
    """Fitness / Fatigue / Form the way Strava builds them: exponentially
    weighted averages of daily Relative Effort over 42 and 7 days (all sports).
    The method is Strava's; their exact scaling is not public, so the
    numbers are close to, not identical with, the app's."""
    load = (state.get("strava") or {}).get("load") or {}
    if not load:
        return None
    by_day = {}
    for a in load.values():
        if a.get("re"):
            by_day[a["date"]] = by_day.get(a["date"], 0) + a["re"]
    if not by_day:
        return None
    day, today = d(min(by_day)), now().date()
    k_fit, k_fat = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    fit = fat = 0.0
    series = []
    while day <= today:
        re_ = by_day.get(day.isoformat(), 0)
        fit += (re_ - fit) * k_fit
        fat += (re_ - fat) * k_fat
        series.append([day.isoformat(), round(fit, 1), round(fat, 1), round(fit - fat, 1), re_])
        day += dt.timedelta(days=1)
    week_ago = series[-8] if len(series) > 7 else series[0]
    return {"series": series, "fitness": series[-1][1], "fatigue": series[-1][2], "form": series[-1][3],
            "fitness_7d": round(series[-1][1] - week_ago[1], 1), "since": series[0][0]}


RACES = [("5K", 5000), ("10K", 10000), ("Half", 21097.5), ("Marathon", 42195)]


def strava_predictions(runs):
    """Riegel (T2 = T1 * (D2/D1)^1.06) from the best efforts Strava found in
    the last 6 weeks. Efforts inside training runs aren't all-out, so these
    are conservative."""
    since = (now().date() - dt.timedelta(days=42)).isoformat()
    efforts = []
    for r in runs:
        if r["date"] < since:
            continue
        for name, secs, dist in r.get("best_efforts") or []:
            if secs and dist and dist >= 1600:
                efforts.append((name, secs, dist, r["date"]))
    if not efforts:
        return None
    out = []
    for race, dist in RACES:
        best = min(efforts, key=lambda e: e[1] * (dist / e[2]) ** 1.06)
        out.append({"race": race, "secs": round(best[1] * (dist / best[2]) ** 1.06),
                    "from": f"{best[0]} {dur_str(best[1])} on {best[3]}"})
    return out


def claude_bin():
    import shutil
    for c in (config().get("claude_bin"), shutil.which("claude"), HOME / ".local/bin/claude", HOME / ".claude/local/claude",
              HOME / ".local/share/mise/command-wrappers/bin/claude"):
        if c and Path(c).exists():
            return str(c)
    return None


NOTE_SYSTEM = (
    "You are a running coach writing a short note on one run for your athlete, who is following a "
    "training plan toward a race. Use only the data given. 2-3 sentences, under 60 words, plain text, no "
    "markdown, no greeting, no emojis. Say what went well or not against the planned session and "
    "one concrete thing to take into the next runs. Pace is min:sec per km, HR in bpm.")


def write_notes(state, plan, st, limit=3):
    """A coach note per new run, written by Claude Code (headless, no tools).
    At most `limit` per scan, runs from the last 14 days only."""
    if not config().get("coach_notes", True) or not claude_bin():
        return 0
    import subprocess
    notes = state.setdefault("notes", {})
    since = (now().date() - dt.timedelta(days=14)).isoformat()
    todo = [dd for w in reversed(st.get("weeks") or []) for dd in reversed(w["days"])
            if dd.get("run") and dd["date"] >= since and dd["date"] not in notes
            and "garmin" in dd["run"].get("source", "")]  # Strava data is never sent to an AI model
    made = 0
    for dd in todo[:limit]:
        r = dict(dd["run"])
        r.pop("note", None)
        ch = r.pop("chart", None) or {}
        r.pop("splits", None)
        for k in ("strava", "name", "source"):
            r.pop(k, None)
        brief = {
            "goal": f"{plan.goal['race']} on {plan.goal['date']} in {plan.goal['target_time']}",
            "plan_week": st["week"]["number"] if dd in st["week"]["days"] else None,
            "hr_zones_bpm": {b["name"]: b["label"] for b in plan.bands}, "easy_cap_bpm": plan.zones["easy"][1],
            "date": dd["date"], "planned_session": {"id": dd.get("id"), "title": dd.get("title"), "type": dd.get("type"),
                                                     "steps": dd.get("steps")},
            "run": r, "stages": ch.get("stages"),
        }
        try:
            res = subprocess.run(
                [claude_bin(), "-p", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
                 "--setting-sources", ""] + (["--model", config()["note_model"]] if config().get("note_model") else []) + [
                 "--system-prompt", NOTE_SYSTEM, json.dumps(brief, default=str)],
                capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
            text = res.stdout.strip()
            if res.returncode == 0 and text:
                notes[dd["date"]] = {"text": text[:600], "made": now().isoformat(timespec="minutes")}
                made += 1
                state.pop("notes_error", None)
            else:
                state["notes_error"] = (res.stderr or res.stdout or "no output").strip()[:200]
                break
        except Exception as e:
            state["notes_error"] = f"{type(e).__name__}: {e}"[:200]
            break
    return made


def plan_html():
    p = config().get("plan_html")
    p = Path(os.path.expanduser(p)) if p else CONF_DIR / "plan.html"
    return str(p) if p.exists() else None


def setup_error(reason):
    return {"version": 1, "generated": now().isoformat(timespec="seconds"),
            "bar": {"text": "run-coach: check setup", "class": "setup", "glyph": GLYPH["setup"], "tooltip": reason},
            "problems": [reason]}


def rebuild(do_scan=False, force=False):
    state = load_json(STATE, {})
    raw = load_json(PLAN, None)
    if not raw or "sessions" not in raw:
        st = setup_error(f"No plan yet: run `run-coach init-plan --race-date YYYY-MM-DD` (or copy one to {PLAN})")
        save_json(STATUS, st)
        return st
    plan = Plan(raw, state.get("overrides", {}))
    problems = []
    if do_scan:
        for err in (scan_garmin(state, plan.start, force), scan_fit(state, plan.start), scan_strava(state, plan.start)):
            if err:
                problems.append(err)
        state["last_scan"] = now().isoformat()
        save_json(STATE, state, 0o600)
    elif activity_dir() and not activity_dir().is_dir():
        problems.append(f"activity folder missing: {activity_dir()}")
    try:
        st = build_status(state, plan, problems)
        if do_scan and "weeks" in st:
            save_json(STATUS, st)  # show the scan before waiting on notes
            if write_notes(state, plan, st):
                save_json(STATE, state, 0o600)
                st = build_status(state, plan, problems)
    except Exception as e:
        st = setup_error(f"{type(e).__name__}: {e}")
    save_json(STATUS, st)
    return st


# ---------------------------------------------------------------- commands

def cmd_mark(args):
    state = load_json(STATE, {})
    raw = load_json(PLAN, None)
    plan = Plan(raw, state.get("overrides", {}))
    sid = args.session
    if not sid:
        st = rebuild()
        sid = (st.get("current") or {}).get("id")
    s = plan.by_id(sid) if sid else None
    if not s:
        sys.exit(f"no such session: {sid}")
    ov = state.setdefault("overrides", {})
    o = ov.setdefault(sid, {})
    if args.action == "done":
        o["status"] = "done"
        if args.km:
            state.setdefault("km_adjust", {})[s["date"]] = state.get("km_adjust", {}).get(s["date"], 0) + args.km
    elif args.action == "skip":
        o["status"] = "skipped"
    elif args.action == "move":
        o["moved_to"] = (d(s["date"]) + dt.timedelta(days=1)).isoformat()
    elif args.action == "clear":
        ov.pop(sid, None)
    if not o:
        ov.pop(sid, None)
    save_json(STATE, state, 0o600)
    rebuild()
    print(f"{sid}: {args.action}")


def cmd_km(args):
    state = load_json(STATE, {})
    adj = state.setdefault("km_adjust", {})
    adj[args.date] = round(adj.get(args.date, 0) + args.delta, 2)
    if not adj[args.date]:
        del adj[args.date]
    save_json(STATE, state, 0o600)
    rebuild()
    print(f"{args.date}: {adj.get(args.date, 0):+} km")


def cmd_init_plan(args):
    """Start from a plan file (the bundled example by default), moved so its
    race lands on --race-date."""
    src = Path(args.source) if args.source else APP / "example-plan.json"
    if PLAN.exists() and not args.force:
        sys.exit(f"{PLAN} already exists (use --force to replace it)")
    raw = load_json(src, None)
    if not raw or "sessions" not in raw:
        sys.exit(f"not a plan: {src}")
    shift = (d(args.race_date) - d(raw["goal"]["date"])).days

    def mv(x):
        return (d(x) + dt.timedelta(days=shift)).isoformat()

    for s_ in raw["sessions"]:
        s_["date"] = mv(s_["date"])
    for c in raw.get("checkpoints", []):
        if c.get("date"):
            c["date"] = mv(c["date"])
        if c.get("dates"):
            c["dates"] = [mv(x) for x in c["dates"]]
    raw["goal"]["date"] = args.race_date
    if args.race_name:
        raw["goal"]["race"] = args.race_name
        raw["goal"]["short"] = args.short or args.race_name
    if args.target:
        raw["goal"]["target_time"] = args.target
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    save_json(PLAN, raw)
    first = min(s_["date"] for s_ in raw["sessions"])
    print(f"Plan saved to {PLAN}\n  {len(raw['sessions'])} sessions from {first} to race day {args.race_date}"
          + ("" if shift % 7 == 0 else f"\n  Note: moved by {shift} days, so sessions fall on different weekdays than the original."))
    print("Next: set your own HR zones in it (zones_hr) and check the paces. See PLAN.md.")
    rebuild()


def cmd_waybar(_):
    st = load_json(STATUS, None) or rebuild()
    b = st.get("bar", {})
    tip = [b.get("tooltip", "")]
    c = st.get("current")
    if c:
        tip += ["", f"{c['dow']} {c['date']} · {c['garmin'] or ''}"] + [f"  {x}" for x in c["steps"]]
    lr = st.get("last_run")
    if lr:
        tip += ["", f"Last run {lr['date']}: {lr['distance_km']} km, {lr['duration']}, {lr['pace']}/km, HR {lr['avg_hr']}",
                f"  {lr['verdict']}"]
    cp = st.get("next_checkpoint")
    if cp:
        tip += [f"Next checkpoint {cp['date']}: {cp['test']} ({cp['target']})"]
    print(json.dumps({"text": f"{b.get('glyph', '')} {b.get('text', '')}", "tooltip": "\n".join(tip),
                      "class": b.get("class", "setup"), "percentage": int(100 * st.get("week", {}).get("pct", 0))}))


def cmd_detail(_):
    st = rebuild()
    b = st["bar"]
    print(f"{b['text']}\n")
    c = st.get("current")
    if c:
        print(f"{c['dow']} {c['date']}  {c['id']} {c['title']}  [{c['status']}]")
        if c.get("garmin"):
            print(f"  Garmin: {c['garmin']}")
        for x in c["steps"]:
            print(f"  · {x}")
    w = st.get("week")
    if w:
        print(f"\nWeek {w['number']}: {w['km']}/{w['target']} km · {w['done']}/{w['planned']} sessions")
        for dd in w["days"]:
            print(f"  {dd['dow']} {dd['day']:>2}  {dd['status']:<11} {dd['km']:>5} km  {dd['title']}")
    lr = st.get("last_run")
    if lr:
        print(f"\nLast run {lr['date']} {lr['time']} ({lr['source']}) · {lr['session']}")
        print(f"  {lr['distance_km']} km · {lr['duration']} · {lr['pace']}/km · HR {lr['avg_hr']} (max {lr['max_hr']})")
        if lr.get("hr_after_10") is not None:
            print(f"  HR after 10 min {lr['hr_after_10']} · {lr['pct_above_cap']}% above {lr['cap']}")
        print(f"  VR {lr.get('vr')}% · cadence {lr.get('cadence')} · GCT {lr.get('gct')} ms · step {lr.get('step_m')} m")
        print(f"  {lr['verdict']}")
        for r in lr.get("reps", []):
            print(f"    {'✓' if r['ok'] else '✗'} {r['name']}: {r['pace']}/km HR {r['hr']} (target {r['target']})")
    r = st.get("race")
    if r:
        print(f"\n{r['days']} days to {r['name']} ({r['date']})")
    cp = st.get("next_checkpoint")
    if cp:
        print(f"Next checkpoint {cp['date']} (in {cp['days']} d): {cp['test']} · {cp['target']}")
    for p in st.get("problems") or []:
        print(f"! {p}")


def cmd_garmin_auth(_):
    if not sys.stdin.isatty():
        sys.exit("This asks for your login details: run it in a terminal window (Super+Enter).")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    import getpass
    from garminconnect import Garmin
    print("Log in to Garmin Connect. Only a login token is saved (not your password):")
    print(f"  {GARMIN_TOKEN}\n")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    g = Garmin(email, password, prompt_mfa=lambda: input("Two-factor code: ").strip())
    GARMIN_TOKEN.unlink(missing_ok=True)
    g.login(tokenstore=str(GARMIN_TOKEN))
    if not GARMIN_TOKEN.exists():
        g.client.dump(str(GARMIN_TOKEN))
    os.chmod(GARMIN_TOKEN, 0o600)
    print(f"Logged in as {g.full_name or g.display_name}. Downloading runs…")
    st = rebuild(do_scan=True, force=True)
    print(st["bar"]["text"])
    for p in st.get("problems") or []:
        print("!", p)


def cmd_strava_auth(_):
    if not sys.stdin.isatty():
        sys.exit("This asks for your login details: run it in a terminal window (Super+Enter).")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    print("Create a Strava API app first: https://www.strava.com/settings/api")
    print("  Authorization Callback Domain: localhost\n")
    cid = input("Client ID: ").strip()
    import getpass
    secret = getpass.getpass("Client Secret: ").strip()
    redirect = f"http://localhost:{STRAVA_PORT}/exchange"
    import secrets
    state_ = secrets.token_urlsafe(16)  # proves the browser redirect answers our own request
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (q.get("state") or [None])[0] != state_:
                self.send_response(400)
                self.end_headers()
                return  # not our authorisation request
            got["code"] = (q.get("code") or [None])[0]
            got["scope"] = (q.get("scope") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Run Coach: Strava connected. You can close this tab.".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", STRAVA_PORT), H)
    srv.timeout = 2

    def serve():  # keep answering until our own redirect arrives (or 5 min)
        end = time.time() + 300
        while not got.get("code") and time.time() < end:
            srv.handle_request()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect,
        "approval_prompt": "auto", "scope": "activity:read_all", "state": state_})
    print(f"\nOpening the browser to authorise…\n{url}\n")
    webbrowser.open(url)
    th.join(300)
    srv.server_close()
    if not got.get("code"):
        sys.exit("No authorisation code received.")
    if "activity:read" not in got.get("scope", ""):
        sys.exit("Strava didn't grant activity access; tick 'View data about your activities'.")
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "code": got["code"],
                                   "grant_type": "authorization_code"}).encode()
    with urllib.request.urlopen("https://www.strava.com/oauth/token", data, timeout=15) as r:
        tok = json.load(r)
    save_json(TOKEN, {"client_id": cid, "client_secret": secret, "access_token": tok["access_token"],
                      "refresh_token": tok["refresh_token"], "expires_at": tok["expires_at"]}, 0o600)
    print("Connected. Syncing…")
    st = rebuild(do_scan=True)
    print(st["bar"]["text"])


def main():
    ap = argparse.ArgumentParser(prog="run-coach")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--force", action="store_true", help="ignore the 3 min Garmin Connect throttle")
    sub.add_parser("status")
    sub.add_parser("waybar")
    sub.add_parser("detail")
    m = sub.add_parser("mark")
    m.add_argument("action", choices=["done", "skip", "move", "clear"])
    m.add_argument("session", nargs="?")
    m.add_argument("--km", type=float)
    k = sub.add_parser("km")
    k.add_argument("date")
    k.add_argument("delta", type=float)
    ip = sub.add_parser("init-plan", help="create your plan from the example, moved to your race date")
    ip.add_argument("--race-date", required=True, help="YYYY-MM-DD")
    ip.add_argument("--race-name")
    ip.add_argument("--short", help="short race name for the bar, e.g. NYC")
    ip.add_argument("--target", help="goal time H:MM:SS")
    ip.add_argument("--source", help="plan file to start from (default: the bundled example)")
    ip.add_argument("--force", action="store_true")
    sub.add_parser("garmin-auth")
    sub.add_parser("garmin-disconnect")
    sub.add_parser("strava-auth")
    sub.add_parser("strava-disconnect")
    args = ap.parse_args()
    if args.cmd == "scan":
        st = rebuild(do_scan=True, force=args.force)
        print(st["bar"]["text"])
        for p in st.get("problems") or []:
            print("!", p, file=sys.stderr)
    elif args.cmd == "status":
        print(json.dumps(rebuild(), indent=1, ensure_ascii=False))
    elif args.cmd == "waybar":
        cmd_waybar(args)
    elif args.cmd == "detail":
        cmd_detail(args)
    elif args.cmd == "mark":
        cmd_mark(args)
    elif args.cmd == "km":
        cmd_km(args)
    elif args.cmd == "init-plan":
        cmd_init_plan(args)
    elif args.cmd == "garmin-auth":
        cmd_garmin_auth(args)
    elif args.cmd == "garmin-disconnect":
        GARMIN_TOKEN.unlink(missing_ok=True)
        state = load_json(STATE, {})
        state.pop("garmin", None)
        save_json(STATE, state, 0o600)
        rebuild()
        print("Garmin Connect disconnected (downloaded files kept).")
    elif args.cmd == "strava-auth":
        cmd_strava_auth(args)
    elif args.cmd == "strava-disconnect":
        TOKEN.unlink(missing_ok=True)
        state = load_json(STATE, {})
        state.pop("strava", None)
        save_json(STATE, state, 0o600)
        rebuild()
        print("Strava disconnected.")


if __name__ == "__main__":
    main()
