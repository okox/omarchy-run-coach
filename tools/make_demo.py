#!/usr/bin/env python3
"""Fill a scratch Run Coach setup with made-up runs, so you can see the
panel without a watch (and so screenshots never show anyone's real data).

    python3 tools/make_demo.py /tmp/rc-demo [--week 5]

then point the engine at it:

    RUN_COACH_HOME=/tmp/rc-demo/app RUN_COACH_CONFIG=/tmp/rc-demo/config \\
    RUN_COACH_NOW=<printed date> run-coach status

Nothing here talks to the network. All numbers are invented.
"""
import argparse
import datetime as dt
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--week", type=int, default=5, help="plan week to stand in (default 5)")
ap.add_argument("--day", type=int, default=1, help="weekday to stand in, 0=Mon (default 1: Tuesday evening)")
ap.add_argument("--seed", type=int, default=7)
a = ap.parse_args()
random.seed(a.seed)

out = Path(a.out)
app, conf = out / "app", out / "config"
app.mkdir(parents=True, exist_ok=True)
conf.mkdir(parents=True, exist_ok=True)
os.environ["RUN_COACH_HOME"] = str(app)
os.environ["RUN_COACH_CONFIG"] = str(conf)
sys.path.insert(0, str(ROOT / "engine"))
import run_coach as rc  # noqa: E402

# The example plan, moved so the race is ~16 weeks out from "today".
plan = json.loads((ROOT / "engine/example-plan.json").read_text())
first = min(dt.date.fromisoformat(s["date"]) for s in plan["sessions"])
week0_mon = first - dt.timedelta(days=first.weekday())
today = dt.date.today()
this_mon = today - dt.timedelta(days=today.weekday())
shift = (this_mon - (week0_mon + dt.timedelta(days=7 * a.week))).days  # whole weeks: weekdays stay put
mv = lambda x: (dt.date.fromisoformat(x) + dt.timedelta(days=shift)).isoformat()  # noqa: E731
for s in plan["sessions"]:
    s["date"] = mv(s["date"])
for c in plan["checkpoints"]:
    if c.get("date"):
        c["date"] = mv(c["date"])
    if c.get("dates"):
        c["dates"] = [mv(x) for x in c["dates"]]
plan["goal"]["date"] = mv(plan["goal"]["date"])
(conf / "plan.json").write_text(json.dumps(plan, indent=1))
(conf / "config.json").write_text(json.dumps({"timezone": "UTC", "coach_notes": False}))
(conf / "strava.json").write_text("{}")         # makes the Strava section show; never used to sync
(conf / "garmin_tokens.json").write_text("{}")  # same for Garmin
today = this_mon + dt.timedelta(days=a.day)
now = dt.datetime.combine(today, dt.time(18, 30))

FIT = 0.0  # the made-up athlete gets a little fitter every week


def hr_pace(hr):
    """Pace this athlete runs at a given HR (s/km)."""
    return 370 - (hr - 160) * 4.2 - FIT


def stages_for(s):
    steps = rc.plan_steps(s.get("steps", "")) if s["type"] not in ("easy", "rest") else []
    if not steps:
        km = [float(x) for x in __import__("re").findall(r"(\d+(?:\.\d+)?) km", s.get("steps", ""))] or [8]
        hr = 164 if s["type"] != "long" else 162
        return [("active", "Easy", km[0] * 1000, hr_pace(hr), hr, {"name": "Easy", "kind": "open", "work": False})]
    out_ = []
    for st in steps:
        name = st["name"]
        low = name.lower()
        kind = ("warmup" if "warm" in low else "cooldown" if "cool" in low else
                "recovery" if any(w in low for w in ("jog", "walk", "recovery")) else
                "interval" if st.get("work") else "active")
        if st["kind"] == "pace":
            pace = (st["lo"] + st["hi"]) / 2 - FIT / 3
            hr = 160 + (370 - pace) / 4.2
        elif st["kind"] == "hr":
            hr = (st["lo"] + st["hi"]) / 2
            pace = hr_pace(hr)
        else:
            hr, pace = (150, 420) if kind == "recovery" else (175, 250)
        val, unit = st["dur"].split() if st["dur"] != "open" else ("1", "min")
        val = float(val)
        dist = val * 1000 if unit == "km" else (val * 60 if unit == "min" else val) / pace * 1000
        out_.append((kind, name, dist, pace, min(hr, 192), st))
    return out_


def make_run(s, start):
    t = 0.0
    dist = 0.0
    hr = 110.0
    hr_s, dist_s, laps = [], [], []
    for i, (kind, name, d_m, pace, hr_t, step) in enumerate(stages_for(s)):
        lap_t0, lap_d0, hr_acc = t, dist, []
        while dist - lap_d0 < d_m:
            hr += (hr_t - hr) * 0.04 + random.uniform(-0.8, 0.8)
            p = pace * random.uniform(0.97, 1.03)
            t += 5
            dist += 5 * 1000 / p
            hr_acc.append(hr)
            hr_s.append((round(t), round(hr)))
            dist_s.append((round(t), round(dist, 1), round(hr), round(10.8 - FIT / 25 + random.uniform(-.3, .3), 2)))
        laps.append({"dist": dist - lap_d0, "time": t - lap_t0, "hr": round(sum(hr_acc) / len(hr_acc)),
                     "vr": 10.8, "cad": 168, "intensity": kind, "step_idx": i,
                     "step": dict(step)})
    return {
        "source": "garmin", "file": f"demo-{s['id']}.fit", "sport": "running",
        "start_utc": start.replace(tzinfo=dt.timezone.utc).isoformat(),
        "distance_m": dist, "timer_s": t, "elapsed_s": t,
        "avg_hr": round(sum(h for _, h in hr_s) / len(hr_s)), "max_hr": max(h for _, h in hr_s),
        "cadence_spm": 168, "vr_pct": round(10.8 - FIT / 25, 2), "vo_cm": 9.4, "gct_ms": 248, "step_m": 1.12,
        "hr_samples": hr_s, "dist_samples": dist_s, "laps": laps, "workout": s["title"],
        "strava_id": str(100000 + random.randint(0, 99999)), "name": f"{s['title']}",
        "description": "", "suffer_score": round(t / 60 * 1.3), "kudos": random.randint(0, 6),
        "gear": "Demo Trainers", "best_efforts": [["5k", round(5 * hr_pace(176)), 5000]],
    }


state = {"fit": {}, "notes": {}, "garmin": {"last_sync": now.isoformat()}, "strava": {"last_sync": now.isoformat()}}
missed = {s["id"] for s in random.sample([s for s in plan["sessions"] if s["type"] == "easy" and s["date"] < today.isoformat()], 2)}
for s in plan["sessions"]:
    if s["date"] > today.isoformat() or s["type"] in ("rest", "race") or s["id"] in missed:
        continue
    FIT = (dt.date.fromisoformat(s["date"]) - week0_mon - dt.timedelta(days=shift)).days / 7 * 2.2
    act = make_run(s, dt.datetime.combine(dt.date.fromisoformat(s["date"]), dt.time(7, 15)))
    state["fit"][f"/demo/{s['id']}.fit"] = {"mtime": 0, "size": 0, "v": rc.FIT_VERSION, "activity": act}

last = max((c["activity"] for c in state["fit"].values()), key=lambda x: x["start_utc"])
state["notes"][last["start_utc"][:10]] = {"text": "Made-up note for the demo: the work reps sat inside their pace band "
                                          "and HR settled within a minute on each jog. Keep the warm-up this easy.",
                                          "made": now.isoformat()}

# Strava-style daily load for the Fitness/Fatigue chart, and Garmin's numbers.
load = {}
for i in range(120):
    day = today - dt.timedelta(days=119 - i)
    re_ = 0 if random.random() < 0.3 else random.randint(40, 90 + i // 3)
    load[f"d{i}"] = {"date": day.isoformat(), "sport": "Run", "re": re_, "name": "", "km": 0}
state["strava"]["load"] = load
preds = {}
for i in range(120):
    day = today - dt.timedelta(days=119 - i)
    m = 14300 - max(0, i - 60) * 14 + random.randint(-40, 40)
    preds[day.isoformat()] = {"m": m, "h": round(m * 0.475), "k10": round(m * 0.21), "k5": round(m * 0.1)}
state["fitness"] = {"fetched": now.timestamp(), "predictions": preds, "latest": {
    "vo2max": 49.1, "vo2max_date": today.isoformat(), "status": "Productive", "acwr": 1.1, "acwr_status": "Optimal",
    "balance": {"phrase": "Balanced", "low": [1180, 963, 1507], "high": [1010, 776, 1320], "anaerobic": [120, 0, 326]},
    "readiness": {"score": 74, "level": "High", "sleep": 82, "recovery_h": 9.0, "hrv_week": 62},
    "hrv": {"night": 64, "week": 62, "status": "Balanced"},
    "endurance": {"score": 6420, "class": "Trained", "next": 6500},
    "fitness_age": 36.0, "age": 40, "rhr": 50, "lt_hr": 180, "lt_pace": "4:38",
    "predictions": {"date": today.isoformat(), "marathon": preds[today.isoformat()]["m"],
                    "half": preds[today.isoformat()]["h"], "k10": preds[today.isoformat()]["k10"],
                    "k5": preds[today.isoformat()]["k5"]}}}
rc.save_json(app / "state.json", state)
print(f"Demo written to {out}\nRUN_COACH_NOW={now.isoformat(timespec='minutes')}")
