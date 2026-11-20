# The plan file

Run Coach reads your plan from `~/.config/run-coach/plan.json`. The easiest
start is `run-coach init-plan --race-date YYYY-MM-DD`, which copies the bundled
example ([engine/example-plan.json](engine/example-plan.json)) and moves it to your date.
This page explains each field, so you can adapt it or write your own. A good
trick: give this page and your coach's plan to an AI and ask for the JSON.

## Top level

```json
{
  "goal": { "race": "Spring Marathon", "short": "Marathon", "date": "2027-04-18",
            "target_time": "3:29:59", "goal_pace": "4:58/km", "goal_pace_watch": "4:54/km" },
  "zones_hr": { "recovery": [150, 165], "easy": [160, 170], "steady": [168, 172],
                "threshold": [173, 183], "tempo_vo2": [184, 194], "interval": [195, 210] },
  "weekly_km_target": { "0": 36, "1": 42, "2": 47 },
  "benchmark": { "hr": [160, 165], "km": 5, "start_km": 2,
                 "baseline": { "pace": "6:00/km", "vertical_ratio_pct": 12.0 } },
  "checkpoints": [ ... ],
  "rest_day_note": "Rest day (cross-training or strength is fine)",
  "sessions": [ ... ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `goal.race`, `goal.date`, `goal.target_time` | yes | Race name, date (`YYYY-MM-DD`) and goal time (`H:MM:SS`). |
| `goal.short` | no | Short name for the bar and countdown ("days to NYC"). Defaults to `race`. |
| `goal.goal_pace`, `goal.goal_pace_watch` | no | Shown with the countdown. The watch pace can be a little faster, since watches tend to measure long. |
| `zones_hr` | yes | **Your** HR zones in bpm, all six keys. The top of `easy` is the cap easy runs are held to; the top of `recovery` is the cap for sessions with "recovery" in the title. Zones may overlap; for time-in-zone each band starts just above the previous one. |
| `weekly_km_target` | yes | Planned km per plan week (`"0"`, `"1"`, …). Drives the week bar and the weekly-km chart. |
| `benchmark` | no | An aerobic benchmark: a block at a fixed HR whose pace should improve. `hr` is the band, `km` its length, `start_km` where it starts if the watch didn't lap it, `baseline` your starting point. Defaults to 5 km at 160-165 starting at km 2. |
| `checkpoints` | no | Tests along the way, shown as "next checkpoint". Each has `date` (or `dates`), `test` and `on_track` (what good looks like). A checkpoint whose `test` contains "benchmark" marks the `test` sessions on its dates as benchmarks. |
| `rest_day_note` | no | Text shown on rest days. |

## Sessions

One entry per day of the plan:

```json
{ "date": "2026-12-26", "week": 0, "phase": "Kick-off", "id": "W00-3",
  "title": "Parkrun progression", "type": "key",
  "steps": "1.5 km Jog to parkrun (HR 150-170) | 1 km Km 1 at 5:20 (5:25-5:15/km) | 1.5 km Jog home (HR 150-170)",
  "garmin_workout": "W00-3 Parkrun progression" }
```

| Field | Meaning |
|---|---|
| `date` | `YYYY-MM-DD`. |
| `week`, `phase` | Plan week number (0, 1, …) and a phase name ("Base", "Build", "Taper"…). |
| `id` | Unique and stable: your Done / Skip / Move marks are kept by id. |
| `title` | Shown in the bar. Put "recovery" in it to use the recovery HR cap. |
| `type` | `easy`, `long`, `key`, `test`, `rest` or `race`. Easy and long runs are judged by HR against the cap; `key` and `test` sessions rep by rep against their steps. |
| `steps` | The workout (format below). Shown in the panel; for `key` and `test` sessions it is also what laps are matched against. For easy runs it can be free text. |
| `garmin_workout` | Optional: the name of the matching workout on your watch, shown as a reminder. |

### Steps

Steps are separated by ` | `. Each one is:

```
<amount> <km|min|s> <name> (<target>)
```

where the target is `HR 150-170`, a pace range `4:50-4:40/km`, or `by feel`.
Repeats are `N x [step, step]`:

```
2 km Warm up easy (HR 150-170) | 3 x [8 min Threshold (4:50-4:40/km), 2 min Jog recovery (by feel)] | 1.5 km Cool down easy (HR 150-170)
```

A step counts as a *work* rep when it has a pace target and isn't a warm-up,
cool-down, jog or walk, or when it has an HR target and "benchmark" in its
name. Those are the reps judged as on or off target.

Tip: if you also load the same structure onto the watch as a workout, the
watch records which step each lap belongs to, and the matching is exact.
