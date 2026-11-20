#!/bin/bash
# Remove Run Coach. Keeps your plan, history and logins unless --purge.
ID=okox.run-coach
systemctl --user disable --now run-coach-scan.path run-coach-scan.timer 2>/dev/null
rm -f "$HOME"/.config/systemd/user/run-coach-scan.*
systemctl --user daemon-reload
omarchy plugin disable "$ID" 2>/dev/null
rm -rf "$HOME/.config/omarchy/plugins/$ID" "$HOME/.local/bin/run-coach" "$HOME/.local/state/run-coach"
if [[ ${1:-} == --purge ]]; then
  rm -rf "$HOME/.local/share/run-coach" "$HOME/.config/run-coach"
  echo "Run Coach removed, with your plan, history and logins."
else
  echo "Run Coach removed. Kept your plan and logins (~/.config/run-coach) and history (~/.local/share/run-coach)."
  echo "Delete those too with: $0 --purge  (run it before removing this folder)"
fi
