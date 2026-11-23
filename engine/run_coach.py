#!/usr/bin/env python3
"""Run Coach: keeps a running plan in the Omarchy bar.

Reads the Garmin activity FIT files and/or Strava and, when there is one,
plan.json; judges each run against its planned session and writes status.json, which the bar
widget (okox.run-coach) renders.

Subcommands:
  scan                 import new FIT files / Strava runs, rewrite status.json
  status               print status.json (rebuilds it first, no network)
  waybar               print {"text","tooltip","class","percentage"}
  detail               print the current session and the last run as text
  mark done|skip|move|clear [SESSION_ID] [--km N]
  new-plan             create a plan: Claude writes or converts one, or the built-in example
  edit-plan            edit the plan in $EDITOR, checked before it's saved
  check-plan [FILE]    check a plan file
  init-plan --race-date D  the built-in example, moved to D (no questions)
  dashboard [--open|--serve]  the full panel as a web page (any desktop, or your phone)
  install-timer        systemd user timer that refreshes every 15 min (non-Omarchy installs)
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
import shutil
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


def asset(name):
    for base in (Path(__file__).resolve().parent, Path(sys.prefix) / "share/run-coach"):
        if (base / name).exists():
            return base / name
    return None


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
FIT_VERSION = 4  # bump to re-decode cached FIT files after a decoder change
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

STEP_RE = re.compile(r"^(?:([\d.]+) (km|min|s)|open) (.*?) \((HR (\d+)-(\d+)|(\d+:\d\d)-(\d+:\d\d)/km|by feel)\)$")
REST_WORDS = ("warm", "cool", "jog", "recovery", "walk", "easy", "stride")


def parse_step(txt):
    m = STEP_RE.match(txt.strip())
    if not m:
        return None
    val, unit, name, _, hlo, hhi, p1, p2 = m.groups()
    step = {"name": name, "dur": f"{val} {unit}" if val else "open", "kind": "open"}
    if hlo:
        step.update(kind="hr", lo=int(hlo), hi=int(hhi))
    elif p1:
        a, b = sorted((pace_sec(p1), pace_sec(p2)))
        step.update(kind="pace", lo=a, hi=b)  # lo = fastest, hi = slowest (s/km)
    step["work"] = is_work(step["kind"], name)
    return step


def is_work(kind, name):
    """A rep to judge: a pace or HR target on anything but a warm-up,
    cool-down, jog, walk, easy or recovery step (a benchmark always counts)."""
    low = name.lower()
    if kind == "hr" and "benchmark" in low:
        return True
    return kind in ("pace", "hr") and not any(w in low for w in REST_WORDS)


def plan_steps(steps_text):
    """Expand '2 km Warm up (HR 150-170) | 3 x [8 min T (4:50-4:40/km), 2 min Jog (by feel)]'."""
    flat = []
    for item in (steps_text or "").split(" | "):
        item = item.strip()
        rep = re.match(r"^(\d+) x \[(.*)\]$", item)
        if rep:
            inner = [parse_step(x) for x in re.split(r",\s(?=[\d.]+ (?:km|min|s) |open )", rep.group(2))]
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

PLAN_TYPES = {"easy", "long", "key", "test", "rest", "race"}


def check_plan(raw):
    """Everything wrong with a plan, as sentences a person can act on."""
    errs = []
    if not isinstance(raw, dict):
        return ["the file is not a JSON object"]
    g = raw.get("goal")
    if not isinstance(g, dict):
        errs.append('"goal" is missing')
    else:
        for k in ("race", "date", "target_time"):
            if not g.get(k):
                errs.append(f'goal.{k} is missing')
        try:
            d(g.get("date", ""))
        except ValueError:
            errs.append(f'goal.date "{g.get("date")}" is not YYYY-MM-DD')
        if g.get("target_time") and not re.fullmatch(r"\d+:\d\d:\d\d", str(g["target_time"])):
            errs.append(f'goal.target_time "{g["target_time"]}" is not H:MM:SS')
    z = raw.get("zones_hr")
    if not isinstance(z, dict):
        errs.append('"zones_hr" is missing')
    else:
        for k, _ in ZONE_ORDER:
            v = z.get(k)
            if not (isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v) and v[0] <= v[1]):
                errs.append(f'zones_hr.{k} must be [low, high] in bpm')
    if not isinstance(raw.get("weekly_km_target"), dict):
        errs.append('"weekly_km_target" is missing (e.g. {"0": 30, "1": 34})')
    ss = raw.get("sessions")
    if not isinstance(ss, list) or not ss:
        errs.append('"sessions" is missing or empty')
        ss = []
    ids = set()
    for i, x in enumerate(ss):
        where = f'session {i + 1}' + (f' ({x.get("id")})' if isinstance(x, dict) and x.get("id") else "")
        if not isinstance(x, dict):
            errs.append(f"{where}: not an object")
            continue
        for k in ("date", "id", "title", "type"):
            if not x.get(k):
                errs.append(f"{where}: {k} is missing")
        try:
            d(x.get("date", ""))
        except ValueError:
            errs.append(f'{where}: date "{x.get("date")}" is not YYYY-MM-DD')
        if x.get("type") and x["type"] not in PLAN_TYPES:
            errs.append(f'{where}: type "{x["type"]}" must be one of {", ".join(sorted(PLAN_TYPES))}')
        if x.get("id") in ids:
            errs.append(f'{where}: id "{x["id"]}" is used twice')
        ids.add(x.get("id"))
        if not isinstance(x.get("week", 0), int):
            errs.append(f"{where}: week must be a number")
        if x.get("type") in ("key", "test") and x.get("steps"):
            try:
                plan_steps(x["steps"])
            except Exception as e:
                errs.append(f"{where}: steps can't be read ({e})")
    return errs[:30]


def watch_zones(state, strava=True):
    """HR zones without a plan: the watch's own (from the latest FIT file),
    else Garmin's lactate threshold, else the highest HR seen. Returns
    (bands, easy_cap, recovery_cap, where-from). strava=False leaves Strava
    history out (for anything sent to an AI model)."""
    acts = sorted((c["activity"] for c in state.get("fit", {}).values()
                   if c.get("activity") and (c["activity"].get("hr_zones") or {}).get("bounds")),
                  key=lambda a: a["start_utc"])
    if acts:
        b = acts[-1]["hr_zones"]["bounds"]
        src = "your watch's HR zones"
    else:
        lt = ((state.get("fitness") or {}).get("latest") or {}).get("lt_hr")
        if lt:
            b = [round(lt * f) for f in (0.65, 0.81, 0.90, 0.95, 1.0)] + [round(lt * 1.1)]
            src = f"your lactate threshold ({lt} bpm)"
        else:
            hrs = [c["activity"].get("max_hr") or 0 for c in state.get("fit", {}).values() if c.get("activity")]
            if strava:
                hrs += [a.get("max_hr") or 0 for a in state.get("strava", {}).get("activities", {}).values()]
            mx = max(hrs + [0]) or 190
            b = [round(mx * f) for f in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)]
            src = f"your highest HR seen ({mx} bpm)"
    b = sorted(int(x) for x in b if x)[-6:]
    while len(b) < 6:
        b.append(b[-1] + 10 if b else 200)
    bands = []
    for i in range(5):
        lo = 0 if i == 0 else b[i] + 1
        hi = b[i + 1] if i < 4 else 999
        label = f"<{hi + 1}" if i == 0 else (f"{lo}+" if i == 4 else f"{lo}-{hi}")
        bands.append({"key": f"z{i + 1}", "name": f"Zone {i + 1}", "lo": lo, "hi": hi, "label": label})
    return bands, b[2], b[1], src


def zones_from_lthr(lthr, max_hr=None):
    """Six plan zones from a lactate-threshold HR (the usual %LTHR bands)."""
    def z(a, b):
        return [round(lthr * a), round(lthr * b)]
    top = max(max_hr or 0, round(lthr * 1.12))
    return {"recovery": z(0.81, 0.89), "easy": z(0.86, 0.92), "steady": z(0.91, 0.93),
            "threshold": z(0.94, 0.99), "tempo_vo2": z(1.0, 1.05), "interval": [round(lthr * 1.06), top]}


def lthr_guess(state):
    acts = sorted((c["activity"] for c in state.get("fit", {}).values()
                   if c.get("activity") and (c["activity"].get("hr_zones") or {}).get("lthr")),
                  key=lambda a: a["start_utc"])
    if acts:
        z = acts[-1]["hr_zones"]
        return z["lthr"], z.get("max"), "your watch"
    lt = ((state.get("fitness") or {}).get("latest") or {}).get("lt_hr")
    if lt:
        return lt, None, "Garmin Connect"
    return None, None, None


class NoPlan:
    """Stands in for a plan when there is none: no sessions, no race, the
    watch's zones, and the last 12 weeks as the time window."""
    has_plan = False

    def __init__(self, state):
        self.raw = {}
        self.goal = None
        self.race_date = None
        self.sessions = []
        self.km_target = {}
        self.short = ""
        self.bands, easy_cap, rec_cap, self.zones_source = watch_zones(state)
        self.zones = {"easy": [0, easy_cap], "recovery": [0, rec_cap]}
        today = now().date()
        self.week0_monday = today - dt.timedelta(days=today.weekday()) - dt.timedelta(weeks=11)
        self.start = self.week0_monday
        self.last_week = 10 ** 6
        self.bench = {"hr": [0, 0], "km": 0, "start_km": 0, "baseline": {}}

    def on(self, date):
        return []

    def by_id(self, sid):
        return None

    def week_of(self, date):
        return (date - self.week0_monday).days // 7

    def cap_for(self, s):
        return self.zones["easy"][1]


class Plan:
    has_plan = True
    zones_source = "your plan"

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
    for z in msgs.get("time_in_zone_mesgs") or []:
        if str(z.get("reference_mesg")) == "session" and z.get("hr_zone_high_boundary"):
            act["hr_zones"] = {"bounds": list(z["hr_zone_high_boundary"]), "lthr": z.get("threshold_heart_rate"),
                               "max": z.get("max_heart_rate"), "rest": z.get("resting_heart_rate"),
                               "calc": str(z.get("hr_calc_type") or "")}
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
        # FIT stores bpm + 100 (values up to 100 are % of max HR). Recorded
        # activities from the watch come back already as bpm, workout files
        # as bpm + 100: anything above 220 is the offset form.
        def bpm(v):
            return v - 100 if v > 220 else v
        lo_, hi_ = st["custom_target_heart_rate_low"], st["custom_target_heart_rate_high"]
        if lo_ > 100:
            step.update(kind="hr", lo=bpm(lo_), hi=bpm(hi_))
    elif t == "speed" and st.get("custom_target_speed_low"):
        a, b = sorted((1000 / st["custom_target_speed_low"], 1000 / st["custom_target_speed_high"]))
        step.update(kind="pace", lo=a, hi=b)
    inten = str(st.get("intensity", ""))
    step["work"] = inten not in ("warmup", "cooldown", "recovery", "rest") and is_work(step["kind"], name)
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
    if not plan.has_plan:  # nothing to judge against: describe it
        z = max(zones or [], key=lambda x: x["secs"], default=None)
        out["verdict"] = f"Mostly {z['name']} ({z['pct']}%)" if z and z["secs"] else (
            f"Avg HR {out['avg_hr']}" if out["avg_hr"] else "Done")
        return out
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
            if run and not plan.has_plan:
                status = run["status"]
            elif run and (not main or main["type"] == "rest"):
                status = "extra"  # a run nobody planned
            elif km and status == "none":
                status = "done"
            days.append({
                "date": iso, "dow": dow(day), "day": day.day, "today": day == today, "future": day > today,
                "id": main["id"] if main else "",
                "title": main["title"] if main else (("Run" if not plan.has_plan else "Before plan" if day < plan.start else "Extra run") if run else ""),
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
        label = f"Week {n_}" if plan.has_plan else f"Week of {monday.day} {monday.strftime('%b')}"
        return {"number": n_, "label": label, "phase": phase, "km": round(km_, 1), "target": tgt,
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
    if not plan.has_plan:
        lr_ = runs[-1] if runs else None
        if lr_ and lr_["date"] == today.isoformat():
            cls, text = "done", f"Today {lr_['distance_m'] / 1000:.1f} km" + (f" · {pace_str(lr_['pace'])}/km" if lr_.get("pace") else "")
        elif lr_:
            cls, text = "rest", f"Last run {dow(d(lr_['date']))} {lr_['distance_m'] / 1000:.1f} km"
        else:
            cls, text = "rest", "Run Coach · no runs yet"
    recent_missed = [s for s in plan.sessions if s["status"] == "missed" and 0 < (today - d(s["date"])).days <= 2]
    if not plan.has_plan:
        pass
    elif today == plan.race_date:
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
            last_run["session"] = f"{sess['id']} {sess['title']}" if sess else ("Unplanned" if plan.has_plan else "Run")

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
    next_cp = next((c for c in cps if c["date"] >= today.isoformat()), None) if plan.has_plan else None
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
    if plan.has_plan:
        tooltip_bits.append(f"Week {w['number']}: {w['km']:.1f}/{w['target']} km · {w['done']}/{w['planned']} sessions")
        tooltip_bits.append(f"{(plan.race_date - today).days} days to {plan.short}")
    else:
        tooltip_bits.append(f"This week: {w['km']:.1f} km")
        tooltip_bits.append("No training plan yet: open the panel to add one")
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
        # Every day of the plan, for browsing sessions from the panel.
        "sessions": [dict(sess_out(s), result=None, verdict=(results.get(s["id"]) or {}).get("verdict", ""))
                     for s in plan.sessions],
        "plan": {"exists": plan.has_plan, "path": str(PLAN), "zones_source": plan.zones_source,
                 "can_ai": bool(claude_bin())},
        "week": dict(this_week, phase=this_week["phase"] or (current or {}).get("phase", "")),
        "weeks": weeks,
        # Whole plan, for the weekly-volume chart: target for every week,
        # actual km for the weeks so far.
        "volume": [{"week": n_, "target": t_, "km": next((w["km"] for w in weeks if w["number"] == n_), None),
                    "monday": (plan.week0_monday + dt.timedelta(days=7 * n_)).isoformat(),
                    "phase": next((x["phase"] for x in plan.sessions if x["week"] == n_), "")}
                   for n_, t_ in sorted(plan.km_target.items())] if plan.has_plan else
                  [{"week": w_["number"], "target": 0, "km": w_["km"], "monday": w_["monday"], "phase": "",
                    "label": w_["label"]} for w_ in weeks],
        "fitness": fitness_out(state, plan),
        "strava": {"connected": TOKEN.exists(), "fitness": strava_fitness(state),
                   "predictions": strava_predictions(runs), "goal_s": fitness_goal_s(plan) if plan.has_plan else None,
                   "notes_error": state.get("notes_error")} if TOKEN.exists() else None,
        "hr_zones": [{k: b[k] for k in ("key", "name", "label")} for b in plan.bands],
        "last_run": last_run,
        "benchmarks": benches,
        "baseline": {"pace": base.get("pace"), "vr": base.get("vertical_ratio_pct"), "cadence": base.get("cadence_spm"),
                     "gct": base.get("gct_ms"), "vo_cm": base.get("vertical_oscillation_cm")},
        "race": None if not plan.has_plan else {"name": plan.goal["race"], "short": plan.short, "date": plan.goal["date"], "days": (plan.race_date - today).days,
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
    hist = f.get("predictions") or {}
    if plan.has_plan:
        goal_s, race_date, start = fitness_goal_s(plan), plan.goal["date"], plan.start.isoformat()
    else:
        goal_s, race_date, start = None, now().date().isoformat(), None
    return dict(f["latest"], goal_s=goal_s, race_date=race_date, plan_start=start,
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


STRAVA_KEYS = ("strava_id", "name", "description", "suffer_score", "kudos", "gear", "best_efforts")


def garmin_only(state, plan):
    """The day's runs and a plan as seen without any Strava data: Strava-only
    runs dropped, Strava fields stripped from merged ones, and (without a
    plan) HR zones worked out from Garmin data alone."""
    by_date = {}
    for r in merged_runs(state):
        if "garmin" not in r.get("source", ""):
            continue
        r = {k: v for k, v in r.items() if k not in STRAVA_KEYS}
        r["source"] = "garmin"
        by_date.setdefault(r["date"], []).append(r)
    if not plan.has_plan:
        import copy
        plan = copy.copy(plan)
        plan.bands, easy_cap, rec_cap, plan.zones_source = watch_zones(state, strava=False)
        plan.zones = {"easy": [0, easy_cap], "recovery": [0, rec_cap]}
    return by_date, plan


def write_notes(state, plan, st, limit=3):
    """A coach note per new run, written by Claude Code (headless, no tools).
    At most `limit` per scan, runs from the last 14 days only."""
    if not config().get("coach_notes", True) or not claude_bin():
        return 0
    import subprocess
    notes = state.setdefault("notes", {})
    # Strava data is never sent to an AI model: the note is built afresh from
    # the day's Garmin runs alone, not from the panel's combined day.
    garmin_days, gplan = garmin_only(state, plan)
    since = (now().date() - dt.timedelta(days=14)).isoformat()
    todo = [dd for w in reversed(st.get("weeks") or []) for dd in reversed(w["days"])
            if dd.get("run") and dd["date"] >= since and dd["date"] not in notes
            and garmin_days.get(dd["date"])]
    made = 0
    for dd in todo[:limit]:
        sess = gplan.by_id(dd.get("id")) if dd.get("id") else None
        r = analyse(sess, garmin_days[dd["date"]], gplan)
        ch = r.pop("chart", None) or {}
        r.pop("splits", None)
        for k in ("strava", "name", "source"):
            r.pop(k, None)
        brief = {
            "goal": f"{plan.goal['race']} on {plan.goal['date']} in {plan.goal['target_time']}" if plan.has_plan
                    else "no training plan: comment on the run itself",
            "plan_week": st["week"]["number"] if plan.has_plan and dd in st["week"]["days"] else None,
            "hr_zones_bpm": {b["name"]: b["label"] for b in gplan.bands}, "easy_cap_bpm": gplan.zones["easy"][1],
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


DASHBOARD = STATUS_DIR / "dashboard.html"
THEME_KEYS = {"--bg": "background", "--fg": "foreground", "--card": "lighter_background", "--dim": "dark_foreground",
              "--line": "selection", "--accent": "accent", "--green": "green", "--yellow": "yellow", "--red": "red",
              "--orange": "orange", "--cyan": "cyan", "--magenta": "magenta"}


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _blend(top, bottom, a):
    t = [int(top[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bottom[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * a + y * (1 - a)):02x}" for x, y in zip(t, b))


def theme_css():
    """The page follows the desktop: the Omarchy theme's palette, then the
    shell's own popup and menu colours on top (the same ones the bar panel
    uses). Only #rrggbb values are taken, so nothing can be injected."""
    out = {}
    try:
        txt = (HOME / ".local/state/omarchy/current/theme/colors.toml").read_text()
        found = dict(re.findall(r'^\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"', txt, re.M))
        out.update({var: found[key] for var, key in THEME_KEYS.items() if key in found})
    except OSError:
        pass
    try:
        import tomllib
        sh = tomllib.loads((HOME / ".config/omarchy/shell.toml").read_text())
    except (OSError, ValueError, ImportError):
        sh = {}
    pop, menu = sh.get("popups") or {}, sh.get("menu") or {}
    bg = pop.get("background") if HEX.match(str(pop.get("background", ""))) else out.get("--bg", "#1a1b26")
    out["--bg"] = bg
    if HEX.match(str(pop.get("text", ""))):
        out["--fg"] = pop["text"]
    sel = menu.get("selected-background")
    if HEX.match(str(sel or "")):
        a = float(menu.get("selected-background-alpha", 0.22))
        out["--sel"] = sel
        out["--card"] = _blend(sel, bg, max(0.05, a * 0.32))
        out["--line"] = _blend(sel, bg, min(0.45, max(0.12, a * 1.3)))
    if HEX.match(str(menu.get("selected-text", ""))):
        out["--accent"] = out["--title"] = menu["selected-text"]
    return " ".join(f"{k}: {v};" for k, v in out.items())


def write_dashboard(st):
    tpl = asset("dashboard.html")
    if not tpl:
        return None
    data = json.dumps(st, ensure_ascii=False, default=str)
    # Run names and descriptions come from outside: keep them from closing the <script>.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = tpl.read_text().replace("/*THEME*/", theme_css()).replace("/*STATUS*/null", data)
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DASHBOARD.with_suffix(".tmp")
    tmp.write_text(html)
    os.chmod(tmp, 0o600)
    os.replace(tmp, DASHBOARD)
    return DASHBOARD


def cmd_dashboard(args):
    st = rebuild()
    path = write_dashboard(st)
    if not path:
        sys.exit("dashboard.html template not found")
    if not args.serve:
        print(path)
        if args.open:
            webbrowser.open(path.as_uri())
        return
    import hmac
    import secrets
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    files = {"/": (DASHBOARD, "text/html; charset=utf-8"), "/status.json": (STATUS, "application/json")}
    # Other accounts on this computer (or anyone on the network with --lan)
    # can reach the port, so every request needs this run's secret key in its
    # address (?key=…). No cookie: browsers send localhost cookies to every
    # port, so another account's server on localhost could collect it.
    key = secrets.token_urlsafe(24)

    class H(http.server.BaseHTTPRequestHandler):
        def authorised(self, query):
            given = (urllib.parse.parse_qs(query).get("key") or [""])[0]
            return hmac.compare_digest(given.encode(), key.encode())

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if not self.authorised(query):
                self.send_error(403, "Open the link Run Coach printed (it has the key)")
                return
            f = files.get(path)
            if not f or not f[0].exists():
                self.send_error(404)
                return
            body = f[0].read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f[1])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer((host, args.port), H)
    url = f"http://localhost:{args.port}/?key={key}"
    print(f"Run Coach dashboard on {url}  (Ctrl+C to stop)")
    if args.lan:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
                so.connect(("192.0.2.1", 9))  # no packet is sent: just picks the LAN address
                ip = so.getsockname()[0]
        except OSError:
            ip = "<this computer's IP>"
        print(f"On your phone (same Wi-Fi): http://{ip}:{args.port}/?key={key}")
        print("Plain HTTP: the key keeps others out, but use it on a network you trust.")
    print("The key changes every time you start the server.")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_install_timer(_):
    """For installs without setup.sh (pip/pipx): refresh every 15 minutes."""
    import shutil
    exe = shutil.which("run-coach") or os.path.abspath(sys.argv[0])
    units = HOME / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    (units / "run-coach-scan.service").write_text(
        "[Unit]\nDescription=Run Coach: import new runs and refresh\n\n[Service]\nType=oneshot\n"
        f"ExecStart={exe} scan\nNice=10\n")
    (units / "run-coach-scan.timer").write_text(
        "[Unit]\nDescription=Run Coach: refresh every 15 minutes\n\n[Timer]\nOnBootSec=1min\n"
        "OnUnitActiveSec=15min\nOnCalendar=*-*-* 00:01:00\nPersistent=true\nUnit=run-coach-scan.service\n\n"
        "[Install]\nWantedBy=timers.target\n")
    import subprocess
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "run-coach-scan.timer"], check=False)
    print(f"Timer installed: runs `{exe} scan` every 15 minutes.")


def setup_error(reason):
    return {"version": 1, "generated": now().isoformat(timespec="seconds"),
            "bar": {"text": "run-coach: check setup", "class": "setup", "glyph": GLYPH["setup"], "tooltip": reason},
            "problems": [reason]}


def load_plan(state):
    """(plan, problems). No plan file is fine (NoPlan); a broken one is
    (None, [what's wrong])."""
    if not PLAN.exists():
        return NoPlan(state), []
    try:
        raw = json.loads(PLAN.read_text())
    except (OSError, ValueError) as e:
        return None, [f"plan.json can't be read: {e}", "Fix it with `run-coach edit-plan`"]
    errs = check_plan(raw)
    if errs:
        return None, [f"plan.json has {len(errs)} problem(s): " + errs[0], *errs[1:], "Fix it with `run-coach edit-plan`"]
    return Plan(raw, state.get("overrides", {})), []


def rebuild(do_scan=False, force=False):
    state = load_json(STATE, {})
    plan, problems = load_plan(state)
    if plan is None:
        st = setup_error(problems[0])
        st["plan"] = {"exists": True, "path": str(PLAN), "errors": problems, "can_ai": bool(claude_bin())}
        save_json(STATUS, st)
        return st
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
    try:
        write_dashboard(st)
    except OSError:
        pass
    return st


# ---------------------------------------------------------------- commands

def cmd_mark(args):
    state = load_json(STATE, {})
    plan, problems = load_plan(state)
    if plan is None or not plan.has_plan:
        sys.exit(problems[0] if problems else "No plan yet: run `run-coach new-plan`")
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


# ---------------------------------------------------------------- plans

PLAN_SYSTEM = (
    "You write running training plans for Run Coach as one JSON object. Output only the JSON: no prose, "
    "no markdown, no code fences. Follow the format below exactly. One session per calendar day from the "
    "start date to race day inclusive (rest days are sessions with type \"rest\" and empty steps), the race "
    "itself is the last session with type \"race\". Ids are W<week, 2 digits>-<day 1-7>. Weeks start on "
    "Monday at week 0. Use the athlete's HR zones exactly as given in zones_hr, and take every HR target in "
    "the steps from those zones (the numbers in the format examples are only examples). Give paces as m:ss per km, "
    "set from the goal time. "
    "Build volume gradually (about 10% a week at most, a lighter week every 3-4 weeks) and taper for the "
    "last 2-3 weeks. Key and test sessions must use the steps syntax so laps can be matched.\n\n")


def need_tty(what):
    if not sys.stdin.isatty():
        sys.exit(f"{what} asks you questions: run it in a terminal window.")


def ask(prompt, default=None, check=None):
    while True:
        try:
            v = input(f"{prompt}" + (f" [{default}]" if default not in (None, "") else "") + ": ").strip()
        except EOFError:
            sys.exit(1)
        v = v or ("" if default is None else str(default))
        err = check(v) if check else None
        if not err:
            return v
        print(f"  {err}")


def choose(prompt, options):
    """options: [(key, label)], returns the key."""
    print(prompt)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    n = ask("Choose", "1", lambda v: None if v.isdigit() and 1 <= int(v) <= len(options) else "type a number from the list")
    return options[int(n) - 1][0]


def _date_ok(v):
    try:
        return None if d(v) > now().date() else "that date is not in the future"
    except ValueError:
        return "use YYYY-MM-DD"


def _time_ok(v):
    return None if re.fullmatch(r"\d:\d\d:\d\d", v) else "use H:MM:SS, e.g. 3:29:59"


def _hms(v):
    h, m, s_ = map(int, v.split(":"))
    return h * 3600 + m * 60 + s_


def shift_plan(raw, race_date):
    """Move every date so the plan's race lands on race_date (whole weeks
    when possible, so weekdays stay put)."""
    shift = (d(race_date) - d(raw["goal"]["date"])).days

    def mv(x):
        return (d(x) + dt.timedelta(days=shift)).isoformat()

    for s_ in raw["sessions"]:
        s_["date"] = mv(s_["date"])
    for c in raw.get("checkpoints", []):
        if c.get("date"):
            c["date"] = mv(c["date"])
        if c.get("dates"):
            c["dates"] = [mv(x) for x in c["dates"]]
    raw["goal"]["date"] = race_date
    return shift


def scale_paces(raw, ratio):
    """Scale every m:ss pace in the steps and goal paces (a faster goal, faster paces)."""
    def f(m):
        sec = round((int(m.group(1)) * 60 + int(m.group(2))) * ratio)
        return f"{sec // 60}:{sec % 60:02d}"
    pat = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?![\d:])")
    for s_ in raw["sessions"]:
        if s_.get("steps"):
            s_["steps"] = pat.sub(f, s_["steps"])
    for k in ("goal_pace", "goal_pace_watch"):
        if raw["goal"].get(k):
            raw["goal"][k] = pat.sub(f, raw["goal"][k])


def remap_hr(raw, zones):
    """Move every HR number in the plan (steps, titles, benchmark) from the
    plan's own zones onto new ones, mapping zone edge to zone edge."""
    old = raw["zones_hr"]
    pts = sorted({(o, n) for k in old for o, n in zip(old[k], zones[k])})

    def f(x):
        x = int(x)
        if x <= pts[0][0]:
            return x + pts[0][1] - pts[0][0]
        if x >= pts[-1][0]:
            return x + pts[-1][1] - pts[-1][0]
        for (a, na), (b, nb) in zip(pts, pts[1:]):
            if a <= x <= b:
                return round(na + (nb - na) * (x - a) / (b - a)) if b > a else na
        return x
    pat = re.compile(r"(HR\s*(?:<=|≤|<|>)?\s*)(\d{2,3})(?:-(\d{2,3}))?")

    def sub(m):
        return m.group(1) + str(f(m.group(2))) + (f"-{f(m.group(3))}" if m.group(3) else "")
    for s_ in raw["sessions"]:
        for k in ("steps", "title"):
            if s_.get(k):
                s_[k] = pat.sub(sub, s_[k])
    for c in raw.get("checkpoints", []):
        for k in ("test", "on_track"):
            if c.get(k):
                c[k] = pat.sub(sub, c[k])
    if raw.get("benchmark", {}).get("hr"):
        raw["benchmark"]["hr"] = [f(x) for x in raw["benchmark"]["hr"]]
    raw["zones_hr"] = zones


def claude_plan(brief, source_text=None):
    """Ask Claude Code for a plan; up to 2 corrections when it doesn't validate."""
    import subprocess
    fmt = asset("PLAN.md")
    system = PLAN_SYSTEM + "FORMAT:\n" + (fmt.read_text() if fmt else "")
    msg = json.dumps(brief, indent=1)
    if source_text:
        msg += "\n\nConvert this existing plan into the format (keep its sessions, fill the gaps sensibly):\n" + source_text[:60000]
    for attempt in range(3):
        print("  Claude is writing your plan (this can take a few minutes)…" if attempt == 0 else "  Fixing a few problems…",
              flush=True)
        try:
            res = subprocess.run(
                [claude_bin(), "-p", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
                 "--setting-sources", "", "--system-prompt", system, msg],
                capture_output=True, text=True, timeout=900, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            print("  Claude took too long.")
            return None
        out = res.stdout.strip()
        if res.returncode != 0 or not out:
            print("  Claude Code failed: " + (res.stderr or out or "no output").strip()[:300])
            return None
        out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out)
        try:
            raw = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except ValueError as e:
            errs = [f"not valid JSON: {e}"]
            raw = None
        else:
            raw.setdefault("goal", {}).update(brief["goal"])
            raw["zones_hr"] = brief["zones_hr"]
            errs = check_plan(raw)
        if not errs:
            return raw
        msg = (json.dumps(brief, indent=1) + "\n\nYour previous plan had these problems, send the whole corrected JSON:\n- "
               + "\n- ".join(errs) + ("\n\nPrevious plan:\n" + json.dumps(raw) if raw else ""))
    print("  Claude's plan still had problems:\n  - " + "\n  - ".join(errs[:8]))
    return None


def save_plan(raw):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if PLAN.exists():
        bak = PLAN.with_name(f"plan.{now().strftime('%Y%m%d-%H%M%S')}.bak.json")
        shutil.copy2(PLAN, bak)
        print(f"Your previous plan is saved as {bak}")
    save_json(PLAN, raw)


def cmd_new_plan(args):
    need_tty("new-plan")
    state = load_json(STATE, {})
    print("\033[1mNew training plan\033[0m\n"
          "A plan is one session per day up to your race. Run Coach shows the day's session in the bar\n"
          "and checks each run against it. You can change anything later with `run-coach edit-plan`.\n")
    if PLAN.exists() and ask("You already have a plan. Replace it? (y/N)", "n").lower() != "y":
        return
    race = ask("Race name", "Marathon")
    short = ask("Short name for the bar", race if len(race) <= 12 else race.split()[0])
    distance = choose("Distance:", [("marathon", "Marathon"), ("half marathon", "Half marathon"), ("10 km", "10 km"),
                                    ("5 km", "5 km"), ("other", "Other (you'll describe it)")])
    if distance == "other":
        distance = ask("Describe the race (distance, terrain)")
    race_date = ask("Race date (YYYY-MM-DD)", check=_date_ok)
    target = ask("Goal time (H:MM:SS)", check=_time_ok)

    lthr, mx, src = lthr_guess(state)
    if lthr:
        print(f"\nYour lactate threshold heart rate is {lthr} bpm (from {src}). Your zones are built from it.")
    else:
        print("\nYour HR zones are built from your lactate threshold heart rate (LTHR). If you don't know it,\n"
              "leave it empty and give your maximum heart rate instead.")
    v = ask("Lactate threshold HR (bpm)", lthr or "",
            lambda v: None if not v or (v.isdigit() and 120 <= int(v) <= 215) else "a heart rate between 120 and 215, or empty")
    if not v:
        mx_ = ask("Maximum HR (bpm)", mx or "", lambda v: None if v.isdigit() and 140 <= int(v) <= 230 else "a heart rate between 140 and 230")
        mx = int(mx_)
        v = round(mx * 0.89)
    zones = zones_from_lthr(int(v), mx)
    print("Zones: " + ", ".join(f"{k.replace('_', '/')} {lo}-{hi}" for k, (lo, hi) in zones.items()))
    goal = {"race": race, "short": short, "date": race_date, "target_time": target}

    opts = []
    if claude_bin():
        opts += [("ai", "Claude writes a plan for you, from a few questions"),
                 ("convert", "You have a plan (from a coach, a book, a website): Claude converts it")]
    opts += [("example", "Start from the built-in 16-week marathon plan (paces scaled to your goal)")]
    if not claude_bin():
        print("\n(Install Claude Code, the `claude` command, to have a plan written or converted for you.)")
    how = choose("\nHow do you want to build it?", opts)

    raw = None
    if how in ("ai", "convert"):
        brief = {"goal": goal, "distance": distance, "zones_hr": zones,
                 "start_date": (now().date() + dt.timedelta(days=7 - now().weekday())).isoformat(),
                 "today": now().date().isoformat()}
        text = None
        if how == "ai":
            brief["runs_per_week"] = int(ask("Runs per week", "4", lambda v: None if v.isdigit() and 2 <= int(v) <= 7 else "2 to 7"))
            brief["current_weekly_km"] = float(ask("Km per week lately", "30", lambda v: None if re.fullmatch(r"\d+(\.\d+)?", v) else "a number"))
            brief["longest_recent_run_km"] = float(ask("Longest run in the last month (km)", "15", lambda v: None if re.fullmatch(r"\d+(\.\d+)?", v) else "a number"))
            brief["long_run_day"] = ask("Long run day", "Sunday")
            brief["notes"] = ask("Anything else? (injuries, days you can't run, strength days…)", "")
            weeks = max(4, min(24, (d(race_date) - d(brief["start_date"])).days // 7 + 1))
            brief["plan_weeks"] = weeks
            print(f"\nA {weeks}-week plan starting Monday {brief['start_date']}.")
        else:
            f = Path(os.path.expanduser(ask("Path to your plan (a text, Markdown or CSV file)",
                                            check=lambda v: None if Path(os.path.expanduser(v)).is_file() else "file not found")))
            text = f.read_text(errors="replace")
        raw = claude_plan(brief, text)
        if not raw:
            if ask("Use the built-in plan instead? (Y/n)", "y").lower() != "y":
                sys.exit("No plan saved.")
            how = "example"
    if how == "example":
        raw = load_json(asset("example-plan.json"), None)
        ratio = _hms(target) / _hms(raw["goal"]["target_time"])
        shift_plan(raw, race_date)
        scale_paces(raw, ratio)
        remap_hr(raw, zones)
        raw["goal"].update(goal)
        first = min(s_["date"] for s_ in raw["sessions"])
        if first < now().date().isoformat():
            print(f"Note: the race is less than 16 weeks away, so the plan's first weeks ({first}…) are already behind you.")
    save_plan(raw)
    rebuild()
    ss = sorted(raw["sessions"], key=lambda x: x["date"])
    nxt = next((x for x in ss if x["date"] >= now().date().isoformat() and x["type"] != "rest"), None)
    print(f"\n\033[1mPlan saved\033[0m: {len(ss)} days, {ss[0]['date']} to race day {race_date}.")
    if nxt:
        print(f"First session: {nxt['date']} · {nxt['title']}")
    print("Change it any time with `run-coach edit-plan` (or Edit plan in the panel).")


def cmd_edit_plan(args):
    need_tty("edit-plan")
    if not PLAN.exists():
        sys.exit("No plan yet: run `run-coach new-plan`.")
    import subprocess
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or next(
        (e for e in ("nvim", "vim", "nano", "vi") if shutil.which(e)), "vi")
    tmp = Path(tempfile.mkdtemp()) / "plan.json"
    shutil.copy2(PLAN, tmp)
    try:
        while True:
            subprocess.run(editor.split() + [str(tmp)])
            if tmp.read_bytes() == PLAN.read_bytes():
                print("No changes.")
                return
            try:
                raw = json.loads(tmp.read_text())
                errs = check_plan(raw)
            except ValueError as e:
                errs = [f"not valid JSON: {e}"]
            if not errs:
                break
            print("The plan has problems:\n  - " + "\n  - ".join(errs))
            if ask("Edit again? (Y/n — n throws your changes away)", "y").lower() != "y":
                print("Not saved.")
                return
        save_plan(raw)
        rebuild()
        print("Plan saved.")
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def cmd_check_plan(args):
    f = Path(args.file) if args.file else PLAN
    try:
        raw = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"{f}: {e}")
    errs = check_plan(raw)
    if errs:
        sys.exit(f"{f}: {len(errs)} problem(s)\n  - " + "\n  - ".join(errs))
    ss = sorted(raw["sessions"], key=lambda x: x["date"])
    print(f"{f}: OK · {len(ss)} days, {ss[0]['date']} to {ss[-1]['date']} · race {raw['goal']['date']}")


def cmd_init_plan(args):
    """Start from a plan file (the bundled example by default), moved so its
    race lands on --race-date."""
    src = Path(args.source) if args.source else asset("example-plan.json")
    if not src:
        sys.exit("bundled example plan not found; pass --source FILE")
    if PLAN.exists() and not args.force:
        sys.exit(f"{PLAN} already exists (use --force to replace it)")
    raw = load_json(src, None)
    if not raw or "sessions" not in raw:
        sys.exit(f"not a plan: {src}")
    shift = shift_plan(raw, args.race_date)
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
        print(f"\n{w.get('label') or 'Week ' + str(w['number'])}: {w['km']} km" + (f"/{w['target']} km · {w['done']}/{w['planned']} sessions" if w['target'] else ""))
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
    sub.add_parser("new-plan", help="create your plan: Claude writes or converts one, or start from the example")
    sub.add_parser("edit-plan", help="open your plan in $EDITOR; checked before it's saved")
    cp_ = sub.add_parser("check-plan", help="check a plan file for mistakes")
    cp_.add_argument("file", nargs="?")
    db = sub.add_parser("dashboard", help="the full panel as a web page")
    db.add_argument("--open", action="store_true", help="open it in the browser")
    db.add_argument("--serve", action="store_true", help="serve it (live) instead of writing a file")
    db.add_argument("--port", type=int, default=8724)
    db.add_argument("--lan", action="store_true", help="with --serve: reachable from your local network (needs the printed key)")
    sub.add_parser("install-timer", help="systemd user timer, for installs without setup.sh")
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
    elif args.cmd == "dashboard":
        cmd_dashboard(args)
    elif args.cmd == "install-timer":
        cmd_install_timer(args)
    elif args.cmd == "init-plan":
        cmd_init_plan(args)
    elif args.cmd == "new-plan":
        cmd_new_plan(args)
    elif args.cmd == "edit-plan":
        cmd_edit_plan(args)
    elif args.cmd == "check-plan":
        cmd_check_plan(args)
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
