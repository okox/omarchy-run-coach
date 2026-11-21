#!/bin/bash
# Run Coach: install or update the engine, background timers and bar widget.
# Safe to re-run. Your plan, history and logins are never touched.
#
#   omarchy plugin add https://github.com/okox/omarchy-run-coach.git
#   ~/.config/omarchy/plugins/okox.run-coach/setup.sh
#
# or from a clone anywhere: ./setup.sh
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
ID=okox.run-coach
PLUGIN="$HOME/.config/omarchy/plugins/$ID"
APP="$HOME/.local/share/run-coach"
CONF="$HOME/.config/run-coach"
UNITS="$HOME/.config/systemd/user"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# 1. The bar widget: if this isn't already the plugin folder, copy it there.
if [[ $SRC != "$PLUGIN" ]]; then
  say "Installing the bar widget into $PLUGIN"
  mkdir -p "$PLUGIN"
  rsync -a --delete --exclude .git "$SRC/" "$PLUGIN/"
fi

# 2. Python engine in its own venv.
say "Setting up the engine in $APP"
mkdir -p "$APP" "$CONF" "$HOME/.local/bin" "$UNITS"
chmod 700 "$CONF"
DEPS=(garmin-fit-sdk==21.222.0 garminconnect==0.3.2 curl-cffi==0.17.0 requests==2.34.2)
if command -v uv >/dev/null; then
  [[ -x $APP/venv/bin/python ]] || uv venv -q "$APP/venv"
  uv pip install -q --python "$APP/venv/bin/python" "${DEPS[@]}"
else
  [[ -x $APP/venv/bin/python ]] || python3 -m venv "$APP/venv"
  "$APP/venv/bin/pip" install -q "${DEPS[@]}"
fi
rm -f "$APP/example-plan.json"  # now read from the plugin folder
# The command runs the engine straight from the plugin folder, so
# `omarchy plugin update okox.run-coach` updates it too.
cat >"$HOME/.local/bin/run-coach" <<WRAP
#!/bin/sh
exec "$APP/venv/bin/python" -I "$PLUGIN/engine/run_coach.py" "\$@"
WRAP
chmod +x "$HOME/.local/bin/run-coach"

# 3. Background refresh: a timer, plus a folder watcher if you set activity_dir.
cp "$PLUGIN/engine/systemd/run-coach-scan.service" "$PLUGIN/engine/systemd/run-coach-scan.timer" "$UNITS/"
folder=$("$APP/venv/bin/python" -c 'import json,os,sys
try: print(os.path.expanduser(json.load(open(sys.argv[1])).get("activity_dir") or ""))
except Exception: print("")' "$CONF/config.json")
if [[ -n $folder ]]; then
  sed "s|@FOLDER@|$folder|" "$PLUGIN/engine/systemd/run-coach-scan.path.in" >"$UNITS/run-coach-scan.path"
else
  rm -f "$UNITS/run-coach-scan.path"
fi
systemctl --user daemon-reload
systemctl --user enable --now run-coach-scan.timer >/dev/null
[[ -n $folder ]] && systemctl --user enable --now run-coach-scan.path >/dev/null

# 4. On the bar, beside the clock.
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
sleep 1
omarchy plugin enable "$ID" >/dev/null 2>&1 || true
omarchy bar put "$ID" --section centerEnd --index 0 >/dev/null 2>&1 || true

"$HOME/.local/bin/run-coach" status >/dev/null || true
say "Run Coach is installed."
echo
echo "Next, each in a terminal (or from the panel's buttons):"
echo "  run-coach garmin-auth    # download your runs from Garmin Connect"
echo "  run-coach strava-auth    # Strava: see README for the 2-minute API app setup"
[[ -f $CONF/plan.json ]] || echo "  run-coach new-plan       # optional: a training plan for your race"
echo "Coach notes turn on by themselves if Claude Code (the 'claude' command) is installed."
