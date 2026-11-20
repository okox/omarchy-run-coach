import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Run Coach: the day's training session in the bar; a click opens the week,
// the last run judged against its plan, benchmarks and the race countdown.
// Everything is computed by the run-coach CLI into status.json; this file
// only renders it and sends the panel's buttons back to the CLI.
Panel {
  id: root
  moduleName: "okox.run-coach"
  ipcTarget: "run-coach"
  manageIpc: false

  readonly property string home: Quickshell.env("HOME")
  readonly property string coachBin: home + "/.local/bin/run-coach"
  readonly property string statusPath: home + "/.local/state/run-coach/status.json"

  property var st: null
  property string loadError: ""
  property bool busy: false

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // Theme palette, for the state colours the shell's own palette lacks.
  property var palette: ({})
  function pal(name, fallback) { return palette[name] ? palette[name] : fallback }
  readonly property color cGreen: pal("green", "#9ece6a")
  readonly property color cYellow: pal("yellow", "#e0af68")
  readonly property color cRed: pal("red", "#f7768e")
  readonly property color cAccent: pal("accent", Commons.Color.accent)
  readonly property color cCyan: pal("cyan", "#449dab")
  readonly property color cOrange: pal("orange", "#eb927b")
  readonly property color cMagenta: pal("magenta", "#ad8ee6")

  function zoneColor(key) {
    return ({ recovery: cCyan, easy: cGreen, steady: cYellow, threshold: cOrange,
              tempo_vo2: cRed, interval: cMagenta })[key] || dim
  }

  function stateColor(cls) {
    switch (cls) {
    case "done": return cGreen
    case "warn": return cYellow
    case "missed": return cRed
    case "race": case "extra": return cAccent
    case "rest": case "skipped": case "none": return dim
    case "unconfirmed": return Qt.darker(foreground, 1.25)
    default: return foreground
    }
  }

  function statusLabel(s) {
    return ({ planned: "Planned", done: "Done", warn: "Check", missed: "Missed", rest: "Rest",
              skipped: "Skipped", unconfirmed: "No data yet", race: "Race", extra: "Extra run",
              none: "Nothing planned" })[s] || s
  }

  readonly property var barInfo: st && st.bar ? st.bar : ({ text: "run-coach: check setup", "class": "setup", glyph: "󰜎", tooltip: loadError })
  readonly property string barClass: barInfo["class"] || "setup"
  readonly property color barTint: barClass === "planned" || barClass === "setup" ? foreground : stateColor(barClass)
  readonly property bool barGlow: barClass === "warn" || barClass === "missed" || barClass === "race"

  function runCoach(args) {
    Quickshell.execDetached([root.coachBin].concat(args))
  }

  function refresh() {
    if (scanProc.running) return
    root.busy = true
    scanProc.running = true
  }

  function openPlan() {
    if (st && st.plan_html) Quickshell.execDetached(["xdg-open", st.plan_html])
  }

  // ---- data -------------------------------------------------------------

  // Which sections are folded, remembered across openings and restarts.
  property var openMap: ({})
  function isOpen(k) { return openMap[k] !== false }
  function toggleSection(k) {
    var m = {}
    for (var x in openMap) m[x] = openMap[x]
    m[k] = !isOpen(k)
    openMap = m
    uiFile.setText(JSON.stringify(m))
  }
  FileView {
    id: uiFile
    path: root.home + "/.local/state/run-coach/ui.json"
    printErrors: false
    onLoaded: { try { root.openMap = JSON.parse(text()) || {} } catch (e) {} }
  }

  FileView {
    id: statusFile
    path: root.statusPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var parsed = JSON.parse(text())
        if (parsed && parsed.bar) { root.st = parsed; root.loadError = "" }
      } catch (e) {
        // Mid-write or hand-edited: keep the last good status.
        if (!root.st) root.loadError = "status.json unreadable: " + e
      }
    }
    onLoadFailed: if (!root.st) root.loadError = "No status yet: run `run-coach scan` (" + root.statusPath + ")"
  }

  FileView {
    path: root.home + "/.local/state/omarchy/current/theme/colors.toml"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var out = {}
      var lines = text().split("\n")
      for (var i = 0; i < lines.length; i++) {
        var m = lines[i].match(/^\s*([a-z_]+)\s*=\s*"(#[0-9a-fA-F]{6})"/)
        if (m) out[m[1]] = m[2]
      }
      root.palette = out
    }
  }

  Process {
    id: scanProc
    command: [root.coachBin, "scan"]
    onExited: root.busy = false
  }

  // The day rolls over and the session changes even with nothing new to
  // import; the systemd timer handles Strava, this just keeps the text current.
  Timer {
    interval: 15 * 60 * 1000
    running: true
    repeat: true
    onTriggered: root.runCoach(["status"])
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.openPanel() }
    function close(): void { root.close() }
    function toggle(): void { root.togglePanel() }
    function refresh(): string { root.refresh(); return "ok" }
  }

  // ---- panel placement (centred like the weather and calendar panels) ----

  property bool centredOnBar: false

  function inMiddleOfBar() {
    var window = root.QsWindow.window
    if (!window) return false
    var vertical = root.bar && root.bar.vertical
    var at = root.mapToItem(null, root.width / 2, root.height / 2)
    var span = vertical ? window.height : window.width
    var middle = vertical ? at.y : at.x
    return span > 0 && middle > span / 3 && middle < span * 2 / 3
  }

  function openPanel() {
    root.centredOnBar = root.inMiddleOfBar()
    root.open()
    if (root.centredOnBar) Qt.callLater(function() {
      if (root.opened && root.bar) root.bar.setCenterHoverRevealSuppressed(true)
    })
  }

  function togglePanel() {
    if (root.opened) root.close()
    else root.openPanel()
  }

  onOpenedChanged: {
    if (!opened && centredOnBar && bar) bar.setCenterHoverRevealSuppressed(false)
    if (opened) {
      panelFlick.contentY = 0
      root.weekIdx = -1
      root.selDate = ""
      root.refresh()
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
    }
  }

  // ---- bar ----------------------------------------------------------------

  readonly property real maxTextWidth: Math.max(120, Number(setting("maxWidth", 330)))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: !(root.bar && root.bar.vertical)
    fixedWidth: barRow.implicitWidth + scaledHorizontalMargin * 2
    horizontalMargin: 8.75
    tooltipText: root.opened ? "" : (root.barInfo.tooltip || "")

    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.openPlan()
      else if (b === Qt.RightButton) root.refresh()
      else root.togglePanel()
    }

    Row {
      id: barRow
      anchors.centerIn: parent
      spacing: Style.space(6)
      scale: button.pressScale

      Item {
        id: glyphBox
        width: glyph.implicitWidth
        height: glyph.implicitHeight
        anchors.verticalCenter: parent.verticalCenter

        // Faint halo, no box, when the day needs attention.
        Rectangle {
          anchors.centerIn: parent
          width: Math.max(glyph.implicitWidth, glyph.implicitHeight) + 8
          height: width
          radius: width / 2
          opacity: root.barGlow ? 1 : 0
          visible: opacity > 0
          gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 0.5; color: Qt.rgba(root.barTint.r, root.barTint.g, root.barTint.b, 0.14) }
            GradientStop { position: 1.0; color: "transparent" }
          }
          Behavior on opacity { NumberAnimation { duration: Style.duration(180); easing.type: Easing.OutCubic } }
        }

        Text {
          id: glyph
          textFormat: Text.PlainText
          text: root.barInfo.glyph || "󰜎"
          color: root.barTint
          font.family: root.fontFamily
          font.pixelSize: Style.bar.iconFont
          renderType: Text.NativeRendering
          layer.enabled: root.barGlow
          layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: root.barTint
            shadowBlur: 0.8
            shadowOpacity: 0.8
            shadowHorizontalOffset: 0
            shadowVerticalOffset: 0
            autoPaddingEnabled: true
          }
        }
      }

      Text {
        id: barLabel
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: root.barInfo.text || ""
        width: Math.min(implicitWidth, root.maxTextWidth)
        elide: Text.ElideRight
        color: root.barClass === "rest" ? root.dim : button.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        renderType: Text.NativeRendering
      }
    }
  }

  // ---- panel --------------------------------------------------------------

  readonly property var cur: st ? st.current : null
  readonly property var lastRun: st ? st.last_run : null
  readonly property var race: st ? st.race : null
  readonly property var checkpoint: st ? st.next_checkpoint : null
  readonly property var sources: st ? st.sources : null
  readonly property var zones: st && st.hr_zones ? st.hr_zones : []
  readonly property var fit: st && st.fitness ? st.fitness : null
  readonly property var strava: st && st.strava ? st.strava : null

  function formWord(f) {
    if (f > 5) return "Fresh"
    if (f < -10) return "Tired"
    return "Steady"
  }

  // [label, value, hover] for the Strava section
  function stravaStats() {
    var f = root.strava.fitness, out = []
    if (!f) return out
    out.push(["Fitness", f.fitness.toFixed(0) + (f.fitness_7d ? "  (" + (f.fitness_7d > 0 ? "+" : "") + f.fitness_7d.toFixed(0) + " 7d)" : ""),
              "42-day weighted average of your daily Relative Effort (all sports)"])
    out.push(["Fatigue", f.fatigue.toFixed(0), "7-day weighted average of your daily Relative Effort"])
    out.push(["Form", (f.form > 0 ? "+" : "") + f.form.toFixed(0) + " " + formWord(f.form),
              "Fitness minus fatigue. Positive is fresh, very negative is overreaching.\nAim to rise into race day."])
    var wk = 0, ser = f.series
    for (var i = Math.max(0, ser.length - 7); i < ser.length; i++) wk += ser[i][4]
    out.push(["Effort, 7 days", String(Math.round(wk)), "Sum of Relative Effort over the last 7 days"])
    return out
  }

  function hms(sec) {
    if (!sec && sec !== 0) return "-"
    sec = Math.round(sec)
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60
    return (h ? h + ":" + (m < 10 ? "0" : "") : "") + m + ":" + (s < 10 ? "0" : "") + s
  }
  function paceText(sec) { return sec ? Math.floor(sec / 60) + ":" + ("0" + Math.round(sec % 60)).slice(-2) : "-" }

  // [label, value, hover]
  function fitStats() {
    var f = root.fit, out = []
    if (f.vo2max) out.push(["VO2 max", String(f.vo2max), "Running VO2 max, last updated " + shortDate(f.vo2max_date)])
    if (f.predictions) out.push(["Marathon pred.", hms(f.predictions.marathon),
      "Garmin race predictions\n5K " + hms(f.predictions.k5) + " · 10K " + hms(f.predictions.k10)
      + " · Half " + hms(f.predictions.half) + "\nGoal " + hms(f.goal_s) + ": "
      + hms(Math.abs(f.predictions.marathon - f.goal_s)) + (f.predictions.marathon > f.goal_s ? " to go" : " ahead")])
    if (f.readiness) out.push(["Readiness", f.readiness.score + " " + f.readiness.level,
      "Sleep score " + f.readiness.sleep + " · recovery left " + f.readiness.recovery_h + " h"])
    if (f.endurance) out.push(["Endurance", f.endurance.score + " " + f.endurance.class,
      f.endurance.next ? (f.endurance.next - f.endurance.score) + " points to the next level" : ""])
    if (f.fitness_age) out.push(["Fitness age", String(f.fitness_age), "Your age: " + f.age])
    if (f.rhr) out.push(["Resting HR", f.rhr + " bpm", ""])
    if (f.hrv) out.push(["HRV", f.hrv.night + " " + f.hrv.status, "Last night " + f.hrv.night + " ms, 7-day average " + f.hrv.week + " ms"])
    if (f.lt_hr) out.push(["Threshold", f.lt_hr + (f.lt_pace ? " · " + f.lt_pace : ""), "Lactate threshold from the watch: heart rate and pace /km"])
    return out
  }

  // Week browser: -1 is the current week; otherwise an index into st.weeks.
  property int weekIdx: -1
  readonly property var weeks: st && st.weeks ? st.weeks : (st && st.week ? [st.week] : [])
  readonly property int shownIdx: weeks.length === 0 ? -1 : (weekIdx < 0 || weekIdx >= weeks.length ? weeks.length - 1 : weekIdx)
  readonly property var week: shownIdx >= 0 ? weeks[shownIdx] : null

  // The clicked day; with none selected the details show the last run.
  property string selDate: ""
  readonly property var selDay: {
    if (!selDate || !week) return null
    for (var i = 0; i < week.days.length; i++) if (week.days[i].date === selDate) return week.days[i]
    return null
  }
  readonly property var det: selDay ? selDay.run : lastRun

  function shortDate(iso) {
    if (!iso) return ""
    var dd = new Date(iso.length <= 10 ? iso + "T12:00:00" : iso)
    return dd.toLocaleDateString(Qt.locale(Commons.I18n.formatLocale), "ddd d MMM")
  }

  function ago(iso) {
    if (!iso) return ""
    var mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
    if (mins < 1) return "just now"
    if (mins < 60) return mins + " min ago"
    if (mins < 48 * 60) return Math.round(mins / 60) + " h ago"
    return Math.round(mins / 1440) + " days ago"
  }

  function statusMeaning(dd) {
    switch (dd.status) {
    case "done": return "Done, on plan"
    case "warn": return "Done, but off plan"
    case "missed": return "Missed"
    case "unconfirmed": return "No data yet (watch not synced)"
    case "skipped": return "Skipped"
    case "planned": return dd.future ? "Planned" : "Planned for today"
    case "extra": return "Extra run (not in the plan)"
    case "rest": return "Rest day"
    default: return "Nothing planned"
    }
  }

  function dayClickable(dd) { return !!(dd.run || (dd.steps && dd.steps.length)) }

  function dayTip(dd) {
    var lines = [shortDate(dd.date) + (dd.title && dd.type !== "rest" ? "  ·  " + dd.title : "")]
    lines.push(statusMeaning(dd))
    var r = dd.run
    if (r) {
      lines.push(r.distance_km.toFixed(2) + " km  ·  " + r.duration + "  ·  " + r.pace + "/km"
                 + (r.avg_hr ? "  ·  HR " + r.avg_hr : ""))
      if (r.verdict) lines.push(r.verdict)
    }
    if (dayClickable(dd)) lines.push(r ? "Click for details" : "Click to see the workout")
    return lines.join("\n")
  }

  function selectDay(dd) {
    root.selDate = (root.selDate === dd.date || !dayClickable(dd)) ? "" : dd.date
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    centerOnBar: root.centredOnBar
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(1560))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(1000))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: {
        if (root.selDate) root.selDate = ""
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var k = t.toLowerCase()
        if (k === "r") root.refresh()
        else if (k === "p") root.openPlan()
        else if (k === "h") weekBack.go()
        else if (k === "l") weekFwd.go()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        // Qt's default wheel step is a few pixels; make a notch move ~120 px.
        WheelHandler {
          acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
          onWheel: function(ev) {
            var dy = ev.pixelDelta.y !== 0 ? ev.pixelDelta.y * 1.5 : ev.angleDelta.y
            panelFlick.contentY = Math.max(0, Math.min(panelFlick.contentHeight - panelFlick.height, panelFlick.contentY - dy))
            ev.accepted = true
          }
        }

        Column {
            id: column
            x: Style.space(2)
            width: panelFlick.width - Style.space(4)
            spacing: Style.space(12)

          // Setup problem
          PText {
            visible: !root.st || (root.st.problems && root.st.problems.length > 0)
            width: parent.width
            wrapMode: Text.Wrap
            color: root.cYellow
            text: !root.st ? root.loadError : (root.st.problems || []).join("\n")
          }

          // Three columns: the plan, the run, fitness. Wide instead of tall.
          Row {
            id: cols
            width: parent.width
            spacing: Style.space(28)
            readonly property real usable: width - 2 * spacing

            Column {
              id: colPlan
              width: cols.usable * 0.34
              spacing: Style.space(12)
              SectionHead {
                visible: root.cur !== null
                key: "session"
                title: root.st && root.st.current_is_today ? "Today" : "Next session"
              }
              Column {
                width: parent.width
                spacing: Style.space(12)
                visible: root.cur !== null && root.isOpen("session")
                // ---- Session hero ----
                Item {
                  visible: root.cur !== null
                  width: parent.width
                  implicitHeight: heroCol.implicitHeight

                  Text {
                    id: heroGlyph
                    textFormat: Text.PlainText
                    text: root.barInfo.glyph || "󰜎"
                    color: root.barTint
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.displayLarge
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.topMargin: Style.space(2)
                  }

                  Column {
                    id: heroCol
                    anchors.left: heroGlyph.right
                    anchors.leftMargin: Style.space(14)
                    anchors.right: parent.right
                    spacing: Style.space(3)

                    Item {
                      width: parent.width
                      implicitHeight: heroTitle.implicitHeight
                      PText {
                        id: heroTitle
                        width: parent.width - heroStatus.implicitWidth - Style.space(10)
                        elide: Text.ElideRight
                        font.pixelSize: Style.font.heading
                        font.bold: true
                        text: root.cur ? ((root.st.current_is_today ? "Today" : root.cur.dow + " " + root.cur.date.slice(8)) + " · " + root.cur.title) : ""
                      }
                      PText {
                        id: heroStatus
                        anchors.right: parent.right
                        anchors.baseline: heroTitle.baseline
                        color: root.cur ? root.stateColor(root.cur.status) : root.dim
                        font.pixelSize: Style.font.body
                        text: root.cur ? root.statusLabel(root.cur.status) + (root.cur.moved ? " · moved" : "") : ""
                      }
                    }
                    PText {
                      width: parent.width
                      elide: Text.ElideRight
                      color: root.dim
                      font.pixelSize: Style.font.body
                      text: root.cur ? [root.cur.id, "Week " + root.cur.week + " " + root.cur.phase,
                                        root.cur.garmin ? "Watch: " + root.cur.garmin : ""].filter(function(x) { return x }).join("  ·  ") : ""
                    }
                    PText {
                      visible: !!(root.st && root.st.today_rest)
                      color: root.dim
                      font.pixelSize: Style.font.body
                      text: root.st && root.st.rest_note ? root.st.rest_note : "Rest day"
                    }
                    Column {
                      width: parent.width
                      topPadding: Style.space(4)
                      spacing: Style.space(2)
                      Repeater {
                        model: root.cur ? root.cur.steps : []
                        PText {
                          required property var modelData
                          width: parent.width
                          wrapMode: Text.Wrap
                          font.pixelSize: Style.font.body
                          text: "·  " + modelData
                        }
                      }
                    }
                    PText {
                      visible: !!(root.cur && root.cur.result)
                      width: parent.width
                      wrapMode: Text.Wrap
                      color: root.cur && root.cur.result ? root.stateColor(root.cur.result.status) : root.foreground
                      font.pixelSize: Style.font.body
                      text: root.cur && root.cur.result ? root.cur.result.verdict : ""
                    }
                  }
                }

                // ---- Actions ----
                Row {
                  visible: root.cur !== null
                  spacing: Style.space(6)
                  Button {
                    text: "Done"; iconText: "󰄬"; bordered: true; fontSize: Style.font.body
                    onClicked: root.runCoach(["mark", "done", root.cur.id])
                  }
                  Button {
                    text: "Skip"; iconText: "󰒭"; bordered: true; fontSize: Style.font.body
                    onClicked: root.runCoach(["mark", "skip", root.cur.id])
                  }
                  Button {
                    text: "Move to tomorrow"; iconText: "󰁔"; bordered: true; fontSize: Style.font.body
                    onClicked: root.runCoach(["mark", "move", root.cur.id])
                  }
                  Button {
                    visible: !!(root.cur && (root.cur.moved || root.cur.manual))
                    text: "Undo"; iconText: "󰕌"; bordered: true; fontSize: Style.font.body
                    onClicked: root.runCoach(["mark", "clear", root.cur.id])
                  }
                }
              }
              PanelSeparator { foreground: root.foreground }
              // ---- Week ----
              Column {
                visible: root.week !== null
                width: parent.width
                spacing: Style.space(8)

                Row {
                  width: parent.width
                  spacing: Style.space(6)
                  Fold { key: "week"; anchors.verticalCenter: parent.verticalCenter }
                  Item {
                    width: parent.width - Style.space(22)
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.toggleSection("week") }
                    implicitHeight: weekTitle.implicitHeight
                    Row {
                      spacing: Style.space(8)
                      ArrowText {
                        id: weekBack
                        glyph: "󰅁"
                        enabled: root.shownIdx > 0
                        tip: "Previous week (h)"
                        function go() { if (enabled) { root.weekIdx = root.shownIdx - 1; root.selDate = "" } }
                        onActivated: go()
                      }
                      PText {
                        id: weekTitle
                        font.pixelSize: Style.font.heading
                        font.bold: true
                        text: root.week ? "Week " + root.week.number + (root.week.phase ? " · " + root.week.phase : "") : ""
                      }
                      ArrowText {
                        id: weekFwd
                        glyph: "󰅂"
                        enabled: root.shownIdx >= 0 && root.shownIdx < root.weeks.length - 1
                        tip: "Next week (l)"
                        function go() { if (enabled) { root.weekIdx = root.shownIdx + 1; root.selDate = "" } }
                        onActivated: go()
                      }
                    }
                    PText {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      font.pixelSize: Style.font.bodySmall
                      color: root.dim
                      text: root.week ? root.week.km.toFixed(1) + "/" + root.week.target + " km · "
                                        + root.week.done + "/" + root.week.planned + " sessions" : ""
                    }
                  }
                }
                Column {
                  width: parent.width
                  spacing: Style.space(8)
                  visible: root.isOpen("week")

                  // Progress line
                  Item {
                    width: parent.width
                    height: Style.space(4)
                    Rectangle {
                      anchors.fill: parent
                      radius: height / 2
                      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1)
                    }
                    Rectangle {
                      height: parent.height
                      width: root.week ? parent.width * root.week.pct : 0
                      radius: height / 2
                      color: root.cAccent
                      Behavior on width { NumberAnimation { duration: Style.duration(220); easing.type: Easing.OutCubic } }
                    }
                  }

                  Row {
                    id: daysRow
                    width: parent.width
                    Repeater {
                      model: root.week ? root.week.days : []
                      Item {
                        id: dayCell
                        required property var modelData
                        readonly property bool selected: root.selDate === modelData.date
                        width: daysRow.width / 7
                        implicitHeight: dayCol.implicitHeight + Style.space(8)

                        // Faint fill for the hovered or selected day; no box edge.
                        Rectangle {
                          anchors.fill: parent
                          anchors.margins: Style.space(1)
                          radius: Style.cornerRadius
                          color: root.foreground
                          opacity: dayCell.selected ? 0.09 : (dayMouse.containsMouse && root.dayClickable(dayCell.modelData) ? 0.05 : 0)
                          Behavior on opacity { NumberAnimation { duration: Style.duration(120) } }
                        }

                        Column {
                          id: dayCol
                          y: Style.space(4)
                          width: parent.width
                          spacing: Style.space(3)
                          PText {
                            anchors.horizontalCenter: parent.horizontalCenter
                            font.pixelSize: Style.font.bodySmall
                            font.bold: dayCell.modelData.today || dayCell.selected
                            color: dayCell.modelData.today || dayCell.selected ? root.foreground : root.dim
                            text: dayCell.modelData.dow + " " + dayCell.modelData.day
                          }
                          Dot {
                            anchors.horizontalCenter: parent.horizontalCenter
                            status: dayCell.modelData.status
                            ring: dayCell.modelData.today ? root.foreground : root.dim
                          }
                          PText {
                            width: dayCell.width - Style.space(4)
                            anchors.horizontalCenter: parent.horizontalCenter
                            horizontalAlignment: Text.AlignHCenter
                            elide: Text.ElideRight
                            font.pixelSize: Style.font.bodySmall
                            color: dayCell.modelData.type === "rest" || dayCell.modelData.type === "none" ? root.dim : root.foreground
                            text: dayCell.modelData.type === "rest" ? "Rest"
                                  : dayCell.modelData.title
                          }
                          PText {
                            anchors.horizontalCenter: parent.horizontalCenter
                            font.pixelSize: Style.font.bodySmall
                            color: root.dim
                            text: dayCell.modelData.km > 0 ? dayCell.modelData.km.toFixed(1) + " km" : " "
                          }
                        }

                        MouseArea {
                          id: dayMouse
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: root.dayClickable(dayCell.modelData) ? Qt.PointingHandCursor : Qt.ArrowCursor
                          onClicked: root.selectDay(dayCell.modelData)
                        }
                        PanelToolTip {
                          visible: dayMouse.containsMouse
                          text: root.dayTip(dayCell.modelData)
                        }
                      }
                    }
                  }

                  // ---- Weekly volume over the whole plan ----
                  // Outline = planned km, fill = run km. Click a past week to open it.
                  Item {
                    id: vol
                    visible: volBars.count > 0
                    width: parent.width
                    height: Style.space(100)
                    readonly property var vdata: root.st && root.st.volume ? root.st.volume : []
                    readonly property real maxKm: {
                      var m = 1
                      for (var i = 0; i < vdata.length; i++) m = Math.max(m, vdata[i].target, vdata[i].km || 0)
                      return m
                    }
                    readonly property real slot: vdata.length ? width / vdata.length : 0
                    readonly property real gap: Style.space(2)
                    readonly property real plotH: height - volLabel.height - Style.space(4)

                    PText {
                      id: volLabel
                      font.pixelSize: Style.font.bodySmall
                      color: root.dim
                      text: "Weekly km  ·  plan to race day"
                    }

                    Repeater {
                      id: volBars
                      model: vol.vdata
                      Item {
                        id: wk
                        required property var modelData
                        required property int index
                        readonly property bool past: modelData.km !== null && modelData.km !== undefined
                        readonly property bool shown: root.week && root.week.number === modelData.week
                        readonly property real tH: vol.plotH * modelData.target / vol.maxKm
                        readonly property real aH: past ? vol.plotH * modelData.km / vol.maxKm : 0
                        x: index * vol.slot
                        y: vol.height - vol.plotH
                        width: vol.slot
                        height: vol.plotH

                        // Planned km: thin outline, data-end rounded, on the baseline.
                        Rectangle {
                          x: vol.gap / 2; width: parent.width - vol.gap
                          y: parent.height - wk.tH; height: wk.tH
                          radius: Math.min(Style.space(4), width / 2)
                          color: "transparent"
                          border.width: 1
                          border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, wk.shown ? 0.55 : 0.22)
                        }
                        // Run km.
                        Rectangle {
                          visible: wk.aH > 0
                          x: vol.gap / 2; width: parent.width - vol.gap
                          y: parent.height - wk.aH; height: wk.aH
                          radius: Math.min(Style.space(4), width / 2)
                          color: root.cAccent
                          opacity: wk.shown ? 1 : (wkMouse.containsMouse ? 0.85 : 0.6)
                        }
                        MouseArea {
                          id: wkMouse
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: wk.past ? Qt.PointingHandCursor : Qt.ArrowCursor
                          onClicked: {
                            if (!wk.past) return
                            for (var i = 0; i < root.weeks.length; i++)
                              if (root.weeks[i].number === wk.modelData.week) { root.weekIdx = i; root.selDate = "" }
                          }
                        }
                        PanelToolTip {
                          visible: wkMouse.containsMouse
                          text: "Week " + wk.modelData.week + (wk.modelData.phase ? " · " + wk.modelData.phase : "")
                                + "  (" + root.shortDate(wk.modelData.monday) + ")\n"
                                + (wk.past ? wk.modelData.km.toFixed(1) + " of " + wk.modelData.target + " km planned"
                                           : wk.modelData.target + " km planned")
                        }
                      }
                    }
                  }
                }
              }
              PanelSeparator { foreground: root.foreground }
              // ---- Race + next checkpoint ----
              SectionHead { key: "race"; title: "Race" }
              Item {
                width: parent.width
                implicitHeight: raceRow.implicitHeight
                visible: root.isOpen("race")
                Column {
                  id: raceRow
                  width: parent.width
                  spacing: Style.space(8)

                  Column {
                    width: parent.width
                    spacing: Style.space(4)
                    Row {
                      spacing: Style.space(6)
                      PText {
                        font.pixelSize: Style.font.displayLarge
                        font.bold: true
                        color: root.cAccent
                        text: root.race ? String(root.race.days) : ""
                      }
                      PText {
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: Style.space(4)
                        font.pixelSize: Style.font.body
                        color: root.dim
                        text: "days to " + (root.race && root.race.short ? root.race.short : "race day")
                      }
                    }
                    PText {
                      width: parent.width
                      elide: Text.ElideRight
                      font.pixelSize: Style.font.body
                      text: root.race ? root.shortDate(root.race.date) + "  ·  " + root.race.target + " (" + root.race.pace + ")" : ""
                    }
                  }

                  Column {
                    visible: root.checkpoint !== null
                    width: parent.width
                    spacing: Style.space(2)
                    PText {
                      width: parent.width
                      wrapMode: Text.Wrap
                      font.pixelSize: Style.font.bodySmall
                      color: root.dim
                      text: root.checkpoint ? "Next checkpoint " + (root.checkpoint.days === 0 ? "today" : "in " + root.checkpoint.days + " d") + " (" + root.shortDate(root.checkpoint.date) + ")" : ""
                    }
                    PText {
                      width: parent.width
                      wrapMode: Text.Wrap
                      font.pixelSize: Style.font.body
                      text: root.checkpoint ? root.checkpoint.test + "\n" + root.checkpoint.target : ""
                    }
                  }
                }
              }
            }

            Column {
              id: colRun
              width: cols.usable * 0.35
              spacing: Style.space(12)
              // ---- Run details: the clicked day, else the last run ----
              Column {
                visible: root.det !== null || root.selDay !== null
                width: parent.width
                spacing: Style.space(8)

                Row {
                  width: parent.width
                  spacing: Style.space(6)
                  Fold { key: "run"; anchors.verticalCenter: parent.verticalCenter }
                  Item {
                    width: parent.width - Style.space(22)
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.toggleSection("run") }
                    implicitHeight: lrTitle.implicitHeight
                    PText {
                      id: lrTitle
                      font.pixelSize: Style.font.heading
                      font.bold: true
                      text: root.selDay ? root.shortDate(root.selDay.date) : "Last run"
                    }
                    PText {
                      anchors.right: closeSel.visible ? closeSel.left : parent.right
                      anchors.rightMargin: closeSel.visible ? Style.space(10) : 0
                      anchors.baseline: lrTitle.baseline
                      width: parent.width - lrTitle.implicitWidth - Style.space(12) - (closeSel.visible ? closeSel.width + Style.space(10) : 0)
                      horizontalAlignment: Text.AlignRight
                      elide: Text.ElideRight
                      font.pixelSize: Style.font.body
                      color: root.dim
                      text: root.det ? (root.selDay ? "" : root.shortDate(root.det.date) + " ") + root.det.time + "  ·  " + root.det.session
                            : (root.selDay ? [root.selDay.id, root.selDay.title, root.statusMeaning(root.selDay)].filter(function(x) { return x }).join("  ·  ") : "")
                    }
                    ArrowText {
                      id: closeSel
                      visible: root.selDay !== null
                      anchors.right: parent.right
                      anchors.verticalCenter: lrTitle.verticalCenter
                      glyph: "󰅖"
                      tip: "Back to the last run (Esc)"
                      onActivated: root.selDate = ""
                    }
                  }
                }
                Column {
                  width: parent.width
                  spacing: Style.space(8)
                  visible: root.isOpen("run")

                  // A planned day without a run: its workout.
                  Column {
                    visible: !!(root.selDay && !root.selDay.run)
                    width: parent.width
                    spacing: Style.space(2)
                    PText {
                      visible: !!(root.selDay && root.selDay.garmin)
                      font.pixelSize: Style.font.body
                      color: root.dim
                      text: root.selDay && root.selDay.garmin ? "Watch: " + root.selDay.garmin : ""
                    }
                    Repeater {
                      model: root.selDay && !root.selDay.run ? root.selDay.steps : []
                      PText {
                        required property var modelData
                        width: parent.width
                        wrapMode: Text.Wrap
                        font.pixelSize: Style.font.body
                        text: "·  " + modelData
                      }
                    }
                  }

                  Grid {
                    visible: root.det !== null
                    columns: 4
                    width: parent.width
                    rowSpacing: Style.space(8)
                    Repeater {
                      model: root.det ? [
                        ["Distance", root.det.distance_km.toFixed(2) + " km"],
                        ["Time", root.det.duration],
                        ["Pace", root.det.pace + " /km"],
                        ["Avg HR", (root.det.avg_hr || "-") + (root.det.max_hr ? "  (max " + root.det.max_hr + ")" : "")],
                        ["Above cap", root.det.pct_above_cap !== undefined ? root.det.pct_above_cap + "% > " + root.det.cap : "-"],
                        ["Vertical ratio", root.det.vr ? root.det.vr + "%" : "-"],
                        ["Cadence", root.det.cadence ? root.det.cadence + " spm" : "-"],
                        ["Ground contact", root.det.gct ? root.det.gct + " ms" : "-"]
                      ] : []
                      Column {
                        required property var modelData
                        width: parent.width / 4
                        spacing: Style.space(1)
                        PText { font.pixelSize: Style.font.bodySmall; color: root.dim; text: modelData[0] }
                        PText { font.pixelSize: Style.font.subtitle; text: modelData[1] }
                      }
                    }
                  }

                  PText {
                    visible: root.det !== null
                    width: parent.width
                    wrapMode: Text.Wrap
                    color: root.det ? root.stateColor(root.det.status) : root.foreground
                    font.pixelSize: Style.font.body
                    text: root.det ? root.det.verdict
                          + (root.det.hr_after_10 !== undefined ? "  ·  HR after 10 min " + root.det.hr_after_10 : "")
                          + ((root.det.notes || []).length ? "\n" + root.det.notes.join("\n") : "") : ""
                  }

                  // Coach note (Claude) and what Strava knows about the run
                Column {
                  visible: !!(root.det && (root.det.note || root.det.strava))
                  width: parent.width
                  spacing: Style.space(4)
                  Row {
                    visible: !!(root.det && root.det.note)
                    width: parent.width
                    spacing: Style.space(8)
                    PText { id: noteGlyph; font.pixelSize: Style.font.body; color: root.cAccent; text: "󰭹" }
                    PText {
                      width: parent.width - noteGlyph.width - Style.space(8)
                      wrapMode: Text.Wrap
                      font.pixelSize: Style.font.body
                      text: root.det && root.det.note ? root.det.note : ""
                    }
                  }
                  PText {
                    visible: !!(root.det && root.det.strava)
                    width: parent.width
                    wrapMode: Text.Wrap
                    font.pixelSize: Style.font.bodySmall
                    color: root.dim
                    text: {
                      var v = root.det ? root.det.strava : null
                      if (!v) return ""
                      return ["Strava: " + v.name, v.description, v.relative_effort ? "Relative Effort " + Math.round(v.relative_effort) : "",
                              v.kudos ? v.kudos + " kudos" : "", v.gear || ""].filter(function(x) { return x }).join("  ·  ")
                    }
                  }
                }
                Column {
                    width: parent.width
                    spacing: Style.space(2)
                    visible: !!(root.det && root.det.reps && root.det.reps.length)
                    Repeater {
                      model: root.det && root.det.reps ? root.det.reps : []
                      PText {
                        required property var modelData
                        required property int index
                        font.pixelSize: Style.font.bodySmall
                        color: modelData.ok ? root.foreground : root.cYellow
                        text: (modelData.ok ? "󰄬  " : "󰅖  ") + (index + 1) + ". " + modelData.name + "   " + modelData.pace
                              + "/km  HR " + (modelData.hr || "-") + "   target " + modelData.target
                      }
                    }
                  }

                  // ---- HR zones ----
                  Column {
                    visible: !!(root.det && root.det.zones)
                    width: parent.width
                    spacing: Style.space(6)
                    topPadding: Style.space(4)

                    PText { font.pixelSize: Style.font.body; font.bold: true; text: "Time in HR zones" }

                    // One bar split by zone.
                    Row {
                      id: zoneBar
                      width: parent.width
                      height: Style.space(8)
                      Repeater {
                        model: root.det && root.det.zones ? root.det.zones : []
                        Rectangle {
                          required property var modelData
                          required property int index
                          readonly property real total: {
                            var t = 0
                            var zs = root.det.zones
                            for (var i = 0; i < zs.length; i++) t += zs[i].secs
                            return t || 1
                          }
                          width: zoneBar.width * modelData.secs / total
                          height: zoneBar.height
                          color: root.zoneColor(modelData.key)
                          opacity: 0.85
                          radius: index === 0 || index === root.det.zones.length - 1 ? height / 2 : 0
                          MouseArea { id: segMouse; anchors.fill: parent; hoverEnabled: true }
                          PanelToolTip {
                            visible: segMouse.containsMouse
                            text: modelData.name + " (" + modelData.label + " bpm): " + modelData.time + ", " + modelData.pct + "%"
                          }
                        }
                      }
                    }

                    Grid {
                      columns: 2
                      width: parent.width
                      rowSpacing: Style.space(3)
                      Repeater {
                        model: root.det && root.det.zones ? root.det.zones : []
                        Row {
                          required property var modelData
                          width: parent.width / 2
                          spacing: Style.space(6)
                          Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            width: Style.space(7); height: width; radius: width / 2
                            color: root.zoneColor(modelData.key)
                          }
                          PText {
                            font.pixelSize: Style.font.bodySmall
                            color: modelData.secs > 0 ? root.foreground : root.dim
                            text: modelData.name + " " + modelData.label
                          }
                          PText {
                            font.pixelSize: Style.font.bodySmall
                            color: root.dim
                            text: modelData.time + " · " + modelData.pct + "%"
                          }
                        }
                      }
                    }
                  }

                  // ---- Pace and HR along the run, stages shaded behind ----
                  RunChart {
                    visible: !!(root.det && root.det.chart)
                    width: parent.width
                    chart: root.det && root.det.chart ? root.det.chart : null
                  }

                  PText {
                    visible: root.det !== null
                    font.pixelSize: Style.font.bodySmall
                    color: root.dim
                    text: root.det ? "From " + root.det.source + (root.det.runs > 1 ? " · " + root.det.runs + " runs combined" : "") : ""
                  }
                }
              }
            }

            Column {
              id: colFit
              width: cols.usable * 0.31
              spacing: Style.space(12)
              // ---- Fitness (Garmin's own numbers) ----
              Column {
                visible: root.fit !== null
                width: parent.width
                spacing: Style.space(8)

                Row {
                  width: parent.width
                  spacing: Style.space(6)
                  Fold { key: "fitness"; anchors.verticalCenter: parent.verticalCenter }
                  Item {
                    width: parent.width - Style.space(22)
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.toggleSection("fitness") }
                    implicitHeight: fitTitle.implicitHeight
                    PText { id: fitTitle; font.pixelSize: Style.font.heading; font.bold: true; text: "Garmin" }
                    PText {
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      font.pixelSize: Style.font.bodySmall
                      color: root.dim
                      text: root.fit && root.fit.fetched ? "fitness · updated " + root.ago(root.fit.fetched) : "fitness"
                    }
                  }
                }
                Column {
                  width: parent.width
                  spacing: Style.space(8)
                  visible: root.isOpen("fitness")

                  Grid {
                    columns: 4
                    width: parent.width
                    rowSpacing: Style.space(8)
                    Repeater {
                      model: root.fit ? root.fitStats() : []
                      Item {
                        required property var modelData
                        width: parent.width / 4
                        implicitHeight: statCol.implicitHeight
                        Column {
                          id: statCol
                          spacing: Style.space(1)
                          PText { font.pixelSize: Style.font.bodySmall; color: root.dim; text: modelData[0] }
                          PText { font.pixelSize: Style.font.subtitle; text: modelData[1] }
                        }
                        MouseArea { id: statMouse; anchors.fill: parent; hoverEnabled: true }
                        PanelToolTip { visible: statMouse.containsMouse && modelData[2] !== ""; text: modelData[2] }
                      }
                    }
                  }

                  PText {
                    visible: !!(root.fit && root.fit.balance)
                    width: parent.width
                    wrapMode: Text.Wrap
                    font.pixelSize: Style.font.body
                    color: root.fit && root.fit.balance && root.fit.balance.phrase.indexOf("Balanced") < 0 ? root.cYellow : root.foreground
                    text: root.fit && root.fit.balance ? "Load: " + (root.fit.status ? root.fit.status + " · " : "") + root.fit.balance.phrase : ""
                    MouseArea { id: balMouse; anchors.fill: parent; hoverEnabled: true }
                    PanelToolTip {
                      visible: balMouse.containsMouse
                      text: {
                        var b = root.fit ? root.fit.balance : null
                        if (!b) return ""
                        function row(n, v) { return n + ": " + v[0] + "  (target " + v[1] + "-" + v[2] + ")" }
                        return "Last 4 weeks of training load\n" + row("Low aerobic (easy)", b.low) + "\n"
                               + row("High aerobic (tempo/threshold)", b.high) + "\n" + row("Anaerobic", b.anaerobic)
                               + (root.fit.acwr ? "\nAcute/chronic ratio " + root.fit.acwr + " (" + root.fit.acwr_status + ")" : "")
                      }
                    }
                  }

                  TrendChart {
                    visible: !!(root.fit && root.fit.trend && root.fit.trend.length > 1)
                    width: parent.width
                    fit: root.fit
                  }
                }
              }
              PanelSeparator { foreground: root.foreground; visible: root.fit !== null && root.strava !== null }
              // ---- Strava: Fitness / Fatigue / Form (Strava's method) + predictions ----
              SectionHead {
                visible: root.strava !== null
                key: "strava"
                title: "Strava"
                rightText: "fitness · from your Relative Effort"
                rightTip: "Strava doesn't share its Fitness score, race predictions or AI comments with other apps.\nThese use Strava's method on your Strava data: Fitness = 42-day and Fatigue = 7-day\nweighted averages of daily Relative Effort, Form = Fitness - Fatigue.\nRace predictions: Riegel formula from your best efforts in the last 6 weeks."
              }
              Column {
                visible: root.strava !== null && root.isOpen("strava")
                width: parent.width
                spacing: Style.space(8)

                Grid {
                  columns: 4
                  width: parent.width
                  rowSpacing: Style.space(8)
                  Repeater {
                    model: root.strava ? root.stravaStats() : []
                    Item {
                      required property var modelData
                      width: parent.width / 4
                      implicitHeight: sCol.implicitHeight
                      Column {
                        id: sCol
                        spacing: Style.space(1)
                        PText { font.pixelSize: Style.font.bodySmall; color: root.dim; text: modelData[0] }
                        PText { font.pixelSize: Style.font.subtitle; text: modelData[1] }
                      }
                      MouseArea { id: sMouse; anchors.fill: parent; hoverEnabled: true }
                      PanelToolTip { visible: sMouse.containsMouse; text: modelData[2] }
                    }
                  }
                }

                FitnessChart {
                  visible: !!(root.strava && root.strava.fitness)
                  width: parent.width
                  fitness: root.strava ? root.strava.fitness : null
                }

                PText {
                  visible: !!(root.strava && root.strava.predictions)
                  font.pixelSize: Style.font.body
                  font.bold: true
                  text: "Race predictions"
                }
                Grid {
                  visible: !!(root.strava && root.strava.predictions)
                  columns: 4
                  width: parent.width
                  Repeater {
                    model: root.strava && root.strava.predictions ? root.strava.predictions : []
                    Item {
                      required property var modelData
                      width: parent.width / 4
                      implicitHeight: pCol.implicitHeight
                      Column {
                        id: pCol
                        spacing: Style.space(1)
                        PText { font.pixelSize: Style.font.bodySmall; color: root.dim; text: modelData.race }
                        PText {
                          font.pixelSize: Style.font.subtitle
                          color: modelData.race === "Marathon" && root.strava.goal_s && modelData.secs <= root.strava.goal_s ? root.cGreen : root.foreground
                          text: root.hms(modelData.secs)
                        }
                      }
                      MouseArea { id: pMouse; anchors.fill: parent; hoverEnabled: true }
                      PanelToolTip {
                        visible: pMouse.containsMouse
                        text: "From your best " + modelData.from + "\n(training efforts, so on the slow side)"
                              + (modelData.race === "Marathon" && root.strava.goal_s ? "\nGoal " + root.hms(root.strava.goal_s) : "")
                      }
                    }
                  }
                }
                PText {
                  visible: !!(root.strava && root.strava.notes_error)
                  width: parent.width
                  wrapMode: Text.Wrap
                  font.pixelSize: Style.font.bodySmall
                  color: root.cYellow
                  text: root.strava && root.strava.notes_error ? "Coach notes: " + root.strava.notes_error : ""
                }
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          // ---- Footer ----
          Item {
            width: parent.width
            implicitHeight: footRow.implicitHeight

            PText {
              anchors.left: parent.left
              anchors.right: footRow.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              wrapMode: Text.Wrap
              font.pixelSize: Style.font.bodySmall
              color: root.dim
              text: {
                if (!root.sources) return ""
                var g = (root.sources.garmin_last_activity ? "Garmin: latest run " + root.shortDate(root.sources.garmin_last_activity) : "Garmin: no runs yet")
                      + (!root.sources.garmin_connected ? " · Connect not linked"
                      : (root.sources.garmin_error ? " · Connect: " + root.sources.garmin_error
                      : " · Connect synced " + root.ago(root.sources.garmin_last_sync)))
                var s = !root.sources.strava_connected ? "Strava: not connected"
                      : (root.sources.strava_error ? "Strava: " + root.sources.strava_error
                      : "Strava: synced " + root.ago(root.sources.strava_last_sync))
                return g + "\n" + s
              }
            }

            Row {
              id: footRow
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(6)
              Button {
                text: root.busy ? "Syncing…" : "Refresh"; iconText: "󰑐"; iconSpinning: root.busy
                bordered: true; fontSize: Style.font.body
                onClicked: root.refresh()
              }
              Button {
                text: "Full plan"; iconText: "󰈈"; bordered: true; fontSize: Style.font.body
                enabled: !!(root.st && root.st.plan_html)
                onClicked: { root.openPlan(); root.close() }
              }
            }
          }
        }
      }
    }
  }

  component PText: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.subtitle
  }

  // A day's status dot: filled for a result, an outline ring for planned.
  component Dot: Rectangle {
    property string status: "none"
    property color ring: root.dim
    property real size: Style.space(7)
    width: size; height: size; radius: size / 2
    color: status === "planned" ? "transparent" : root.stateColor(status)
    border.width: status === "planned" ? 1 : 0
    border.color: ring
    opacity: status === "none" ? 0.35 : 1
  }

  // A small clickable glyph (week arrows, close).
  component ArrowText: Text {
    id: arrow
    property string glyph: ""
    property string tip: ""
    signal activated()
    textFormat: Text.PlainText
    text: glyph
    color: arrowMouse.containsMouse && enabled ? root.foreground : root.dim
    opacity: enabled ? 1 : 0.3
    font.family: root.fontFamily
    font.pixelSize: Style.font.heading
    MouseArea {
      id: arrowMouse
      anchors.fill: parent
      anchors.margins: -Style.space(4)
      hoverEnabled: true
      cursorShape: arrow.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: if (arrow.enabled) arrow.activated()
    }
    PanelToolTip { visible: arrowMouse.containsMouse && arrow.tip !== ""; text: arrow.tip }
  }

  // Pace (top) and heart rate (bottom) along the run's distance. Two plots on
  // one x axis, never two y scales on one plot. Stages tint the background.
  component RunChart: Item {
    id: rc
    property var chart: null
    readonly property var pts: chart ? chart.points : []
    readonly property var stages: chart ? chart.stages : []
    readonly property real totalKm: pts.length ? pts[pts.length - 1][0] : 1
    readonly property real plotH: Style.space(64)
    readonly property real gapH: Style.space(10)
    readonly property real labelW: Style.space(44)
    readonly property real plotW: width - labelW
    property real hoverX: -1

    function pct(arr, q) { var a = arr.slice().sort(function(x, y) { return x - y }); return a[Math.min(a.length - 1, Math.floor(q * (a.length - 1)))] }
    readonly property var paceRange: {
      var v = []; for (var i = 0; i < pts.length; i++) if (pts[i][1]) v.push(pts[i][1])
      if (!v.length) return [240, 420]
      var lo = pct(v, 0.02), hi = pct(v, 0.98)
      return [lo - 10, hi + 10]
    }
    readonly property var hrRange: {
      var v = []; for (var i = 0; i < pts.length; i++) if (pts[i][2]) v.push(pts[i][2])
      if (!v.length) return [100, 190]
      return [pct(v, 0.02) - 4, Math.max.apply(null, v) + 3]
    }
    function xOf(km) { return plotW * km / totalKm }
    function stageColor(kind) {
      return ({ warmup: root.cCyan, active: root.cGreen, interval: root.cOrange, recovery: root.dim,
                cooldown: root.cAccent, rest: root.dim })[kind] || root.dim
    }
    function pointAt(px) {
      var km = px / plotW * totalKm, best = null
      for (var i = 0; i < pts.length; i++) if (!best || Math.abs(pts[i][0] - km) < Math.abs(best[0] - km)) best = pts[i]
      return best
    }
    function stageAt(km) {
      for (var i = 0; i < stages.length; i++) if (km >= stages[i].from && km <= stages[i].to) return stages[i]
      return null
    }

    implicitHeight: titleRow.height + Style.space(4) + plotH * 2 + gapH + axisRow.height
    onChartChanged: { paceCanvas.requestPaint(); hrCanvas.requestPaint() }
    onWidthChanged: { paceCanvas.requestPaint(); hrCanvas.requestPaint() }

    Row {
      id: titleRow
      spacing: Style.space(14)
      PText { font.pixelSize: Style.font.body; font.bold: true; text: "Pace & heart rate" }
    }

    // stage tints behind both plots
    Repeater {
      model: rc.stages
      Rectangle {
        required property var modelData
        x: rc.xOf(modelData.from)
        y: titleRow.height + Style.space(4)
        width: Math.max(1, rc.xOf(modelData.to) - rc.xOf(modelData.from))
        height: rc.plotH * 2 + rc.gapH
        color: rc.stageColor(modelData.kind)
        opacity: modelData.kind === "recovery" || modelData.kind === "rest" ? 0.07 : 0.2
      }
    }
    // stage names, on top of the tints (not inside them, so they stay opaque)
    Repeater {
      model: rc.stages
      PText {
        required property var modelData
        readonly property real w: rc.xOf(modelData.to) - rc.xOf(modelData.from)
        visible: w > implicitWidth + Style.space(8)
        x: rc.xOf(modelData.from) + Style.space(4)
        y: titleRow.height + Style.space(6)
        font.pixelSize: Style.font.bodySmall
        color: root.dim
        text: modelData.name
      }
    }

    Canvas {
      id: paceCanvas
      y: titleRow.height + Style.space(4)
      width: rc.plotW; height: rc.plotH
      onPaint: {
        var ctx = getContext("2d"); ctx.reset()
        var lo = rc.paceRange[0], hi = rc.paceRange[1]
        ctx.strokeStyle = root.foreground; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath()
        var pen = false
        for (var i = 0; i < rc.pts.length; i++) {
          var p = rc.pts[i]
          if (!p[1]) { pen = false; continue }
          var v = Math.max(lo, Math.min(hi, p[1]))
          var x = rc.xOf(p[0]), y = (v - lo) / (hi - lo) * height   // faster = higher
          if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y)
          pen = true
        }
        ctx.stroke()
      }
    }
    Canvas {
      id: hrCanvas
      y: paceCanvas.y + rc.plotH + rc.gapH
      width: rc.plotW; height: rc.plotH
      onPaint: {
        var ctx = getContext("2d"); ctx.reset()
        var lo = rc.hrRange[0], hi = rc.hrRange[1]
        ctx.strokeStyle = root.cRed; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath()
        var pen = false
        for (var i = 0; i < rc.pts.length; i++) {
          var p = rc.pts[i]
          if (!p[2]) { pen = false; continue }
          var x = rc.xOf(p[0]), y = height - (p[2] - lo) / (hi - lo) * height
          if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y)
          pen = true
        }
        ctx.stroke()
      }
    }

    // y labels on the right: what each plot is, and its range
    Column {
      x: rc.plotW + Style.space(6); y: paceCanvas.y
      height: rc.plotH
      PText { font.pixelSize: Style.font.bodySmall; color: root.dim; text: root.paceText(rc.paceRange[0]) }
      PText { font.pixelSize: Style.font.bodySmall; color: root.foreground; text: "/km" }
    }
    PText { x: rc.plotW + Style.space(6); y: paceCanvas.y + rc.plotH - height; font.pixelSize: Style.font.bodySmall; color: root.dim; text: root.paceText(rc.paceRange[1]) }
    Column {
      x: rc.plotW + Style.space(6); y: hrCanvas.y
      PText { font.pixelSize: Style.font.bodySmall; color: root.dim; text: String(Math.round(rc.hrRange[1])) }
      PText { font.pixelSize: Style.font.bodySmall; color: root.cRed; text: "bpm" }
    }
    PText { x: rc.plotW + Style.space(6); y: hrCanvas.y + rc.plotH - height; font.pixelSize: Style.font.bodySmall; color: root.dim; text: String(Math.round(rc.hrRange[0])) }

    Item {
      id: axisRow
      y: hrCanvas.y + rc.plotH + Style.space(2)
      width: rc.plotW; height: axisL.implicitHeight
      PText { id: axisL; font.pixelSize: Style.font.bodySmall; color: root.dim; text: "0 km" }
      PText { anchors.right: parent.right; font.pixelSize: Style.font.bodySmall; color: root.dim; text: rc.totalKm.toFixed(1) + " km" }
    }

    // crosshair + tooltip
    Rectangle {
      visible: rc.hoverX >= 0
      x: rc.hoverX; y: paceCanvas.y
      width: 1; height: rc.plotH * 2 + rc.gapH
      color: root.foreground; opacity: 0.6
    }
    MouseArea {
      id: rcMouse
      x: 0; y: paceCanvas.y
      width: rc.plotW; height: rc.plotH * 2 + rc.gapH
      hoverEnabled: true
      onPositionChanged: function(m) { rc.hoverX = m.x }
      onExited: rc.hoverX = -1
    }
    PanelToolTip {
      id: rcTip
      visible: rc.hoverX >= 0
      x: Math.min(rc.hoverX + Style.space(12), rc.width - implicitWidth)
      y: paceCanvas.y - implicitHeight - Style.space(4)
      delay: 0
      text: {
        if (rc.hoverX < 0) return ""
        var p = rc.pointAt(rc.hoverX)
        if (!p) return ""
        var st = rc.stageAt(p[0])
        return "km " + p[0].toFixed(2) + (st ? "  ·  " + st.name + " (" + st.lap + ")" : "")
               + "\nPace " + (p[1] ? root.paceText(p[1]) + "/km" : "-") + "  ·  HR " + (p[2] || "-")
      }
    }
  }

  // Garmin's marathon prediction over time, against the goal, to race day.
  component TrendChart: Item {
    id: tc
    property var fit: null
    readonly property var trend: fit ? fit.trend : []
    readonly property real t0: trend.length ? new Date(trend[0][0] + "T12:00:00").getTime() : 0
    readonly property real t1: fit ? new Date(fit.race_date + "T12:00:00").getTime() : 1
    readonly property var yRange: {
      if (!fit) return [0, 1]
      var lo = fit.goal_s, hi = fit.goal_s
      for (var i = 0; i < trend.length; i++) { lo = Math.min(lo, trend[i][1]); hi = Math.max(hi, trend[i][1]) }
      return [lo - 300, hi + 300]
    }
    readonly property real plotH: Style.space(80)
    readonly property real labelW: Style.space(64)
    readonly property real plotW: width - labelW
    property int hoverIdx: -1
    function xOfT(t) { return plotW * (t - t0) / (t1 - t0) }
    function xOf(iso) { return xOfT(new Date(iso + "T12:00:00").getTime()) }
    function yOf(sec) { return (sec - yRange[0]) / (yRange[1] - yRange[0]) * plotH }  // faster = higher

    implicitHeight: tcTitle.height + Style.space(4) + plotH + tcAxis.height
    onFitChanged: tcCanvas.requestPaint()
    onWidthChanged: tcCanvas.requestPaint()

    PText { id: tcTitle; font.pixelSize: Style.font.body; font.bold: true; text: "Predicted marathon" }

    Canvas {
      id: tcCanvas
      y: tcTitle.height + Style.space(4)
      width: tc.plotW; height: tc.plotH
      onPaint: {
        var ctx = getContext("2d"); ctx.reset()
        if (!tc.fit) return
        // goal: dashed reference line across to race day
        ctx.strokeStyle = root.cGreen; ctx.globalAlpha = 0.8; ctx.lineWidth = 1
        ctx.setLineDash([4, 4]); ctx.beginPath()
        var gy = tc.yOf(tc.fit.goal_s); ctx.moveTo(0, gy); ctx.lineTo(width, gy); ctx.stroke()
        // plan start and today
        ctx.setLineDash([]); ctx.strokeStyle = root.foreground; ctx.globalAlpha = 0.18
        var ps = tc.xOf(tc.fit.plan_start); ctx.beginPath(); ctx.moveTo(ps, 0); ctx.lineTo(ps, height); ctx.stroke()
        // prediction line
        ctx.globalAlpha = 1; ctx.strokeStyle = root.cAccent; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath()
        for (var i = 0; i < tc.trend.length; i++) {
          var x = tc.xOf(tc.trend[i][0]), y = tc.yOf(tc.trend[i][1])
          if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y)
        }
        ctx.stroke()
        var last = tc.trend[tc.trend.length - 1]
        ctx.fillStyle = root.cAccent; ctx.beginPath(); ctx.arc(tc.xOf(last[0]), tc.yOf(last[1]), 4, 0, 2 * Math.PI); ctx.fill()
      }
    }

    // direct labels on the right: the latest prediction and the goal
    PText {
      x: tc.plotW + Style.space(6)
      y: tcCanvas.y + tc.yOf(tc.fit ? tc.fit.goal_s : 0) - height / 2
      font.pixelSize: Style.font.bodySmall; color: root.dim
      text: tc.fit ? "goal " + Math.floor(Math.round(tc.fit.goal_s / 60) / 60) + ":" + ("0" + Math.round(tc.fit.goal_s / 60) % 60).slice(-2) : ""
    }
    PText {
      visible: tc.trend.length > 0
      x: tc.xOf(tc.trend.length ? tc.trend[tc.trend.length - 1][0] : "") + Style.space(8)
      y: tcCanvas.y + tc.yOf(tc.trend.length ? tc.trend[tc.trend.length - 1][1] : 0) - height / 2
      font.pixelSize: Style.font.bodySmall
      text: tc.trend.length ? root.hms(tc.trend[tc.trend.length - 1][1]) : ""
    }

    Item {
      id: tcAxis
      y: tcCanvas.y + tc.plotH + Style.space(2)
      width: tc.plotW; height: tcA.implicitHeight
      PText { id: tcA; font.pixelSize: Style.font.bodySmall; color: root.dim; text: tc.trend.length ? root.shortDate(tc.trend[0][0]) : "" }
      PText {
        x: Math.max(tcA.implicitWidth + Style.space(8), tc.xOf(tc.fit ? tc.fit.plan_start : "") - implicitWidth / 2)
        font.pixelSize: Style.font.bodySmall; color: root.dim; text: "plan start"
      }
      PText { anchors.right: parent.right; font.pixelSize: Style.font.bodySmall; color: root.dim; text: "race day" }
    }

    Rectangle {
      visible: tc.hoverIdx >= 0
      x: tc.hoverIdx >= 0 ? tc.xOf(tc.trend[tc.hoverIdx][0]) : 0
      y: tcCanvas.y; width: 1; height: tc.plotH
      color: root.foreground; opacity: 0.6
    }
    MouseArea {
      x: 0; y: tcCanvas.y; width: tc.plotW; height: tc.plotH
      hoverEnabled: true
      onPositionChanged: function(m) {
        var best = -1, bd = 1e9
        for (var i = 0; i < tc.trend.length; i++) {
          var d = Math.abs(tc.xOf(tc.trend[i][0]) - m.x)
          if (d < bd) { bd = d; best = i }
        }
        tc.hoverIdx = bd < Style.space(12) ? best : -1
      }
      onExited: tc.hoverIdx = -1
    }
    PanelToolTip {
      visible: tc.hoverIdx >= 0
      delay: 0
      x: tc.hoverIdx >= 0 ? Math.min(tc.xOf(tc.trend[tc.hoverIdx][0]) + Style.space(12), tc.width - implicitWidth) : 0
      y: tcCanvas.y - implicitHeight - Style.space(4)
      text: tc.hoverIdx >= 0 ? root.shortDate(tc.trend[tc.hoverIdx][0]) + "\nMarathon " + root.hms(tc.trend[tc.hoverIdx][1])
            + "  (" + root.hms(Math.abs(tc.trend[tc.hoverIdx][1] - tc.fit.goal_s)) + (tc.trend[tc.hoverIdx][1] > tc.fit.goal_s ? " slower" : " faster") + " than goal)" : ""
    }
  }

  // Fold arrow for a section; the open/closed state is remembered.
  component Fold: Text {
    id: fold
    property string key: ""
    textFormat: Text.PlainText
    text: root.isOpen(key) ? "󰅀" : "󰅂"
    color: foldMouse.containsMouse ? root.foreground : root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    MouseArea {
      id: foldMouse
      anchors.fill: parent
      anchors.margins: -Style.space(4)
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.toggleSection(fold.key)
    }
  }

  // A plain section header: fold arrow, title, optional dim text on the right.
  component SectionHead: Item {
    id: sh
    property string key: ""
    property string title: ""
    property string rightText: ""
    property string rightTip: ""
    width: parent ? parent.width : 0
    implicitHeight: shTitle.implicitHeight
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.toggleSection(sh.key) }
    Row {
      spacing: Style.space(6)
      Fold { key: sh.key; anchors.verticalCenter: parent.verticalCenter }
      PText { id: shTitle; font.pixelSize: Style.font.heading; font.bold: true; text: sh.title }
    }
    PText {
      id: shRight
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      font.pixelSize: Style.font.bodySmall
      color: root.dim
      text: sh.rightText
      MouseArea { id: shRightMouse; anchors.fill: parent; hoverEnabled: true }
      PanelToolTip { visible: shRightMouse.containsMouse && sh.rightTip !== ""; text: sh.rightTip }
    }
  }

  // Strava-method Fitness and Fatigue over time; Form in the hover.
  component FitnessChart: Item {
    id: fc
    property var fitness: null
    readonly property var ser: fitness ? fitness.series : []
    readonly property real plotH: Style.space(80)
    readonly property real labelW: Style.space(64)
    readonly property real plotW: width - labelW
    readonly property real maxY: {
      var m = 10
      for (var i = 0; i < ser.length; i++) m = Math.max(m, ser[i][1], ser[i][2])
      return m * 1.1
    }
    property int hoverIdx: -1
    function xOf(i) { return ser.length > 1 ? plotW * i / (ser.length - 1) : 0 }
    function yOf(v) { return plotH - v / maxY * plotH }
    implicitHeight: fcTitle.height + Style.space(4) + plotH + fcAxis.height
    onFitnessChanged: fcCanvas.requestPaint()
    onWidthChanged: fcCanvas.requestPaint()

    PText { id: fcTitle; font.pixelSize: Style.font.body; font.bold: true; text: "Fitness & fatigue, last " + fc.ser.length + " days" }

    Canvas {
      id: fcCanvas
      y: fcTitle.height + Style.space(4)
      width: fc.plotW; height: fc.plotH
      onPaint: {
        var ctx = getContext("2d"); ctx.reset()
        function line(col, idx, alpha) {
          ctx.globalAlpha = alpha; ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath()
          for (var i = 0; i < fc.ser.length; i++) {
            var x = fc.xOf(i), y = fc.yOf(fc.ser[i][idx])
            if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y)
          }
          ctx.stroke()
        }
        line(root.cMagenta, 2, 0.75)   // fatigue
        line(root.cAccent, 1, 1)       // fitness
      }
    }
    // direct labels at the line ends instead of a legend
    PText {
      x: fc.plotW + Style.space(6)
      y: fcCanvas.y + (fc.ser.length ? fc.yOf(fc.ser[fc.ser.length - 1][1]) : 0) - height / 2
      font.pixelSize: Style.font.bodySmall; color: root.cAccent; text: "Fitness"
    }
    PText {
      x: fc.plotW + Style.space(6)
      y: fcCanvas.y + (fc.ser.length ? fc.yOf(fc.ser[fc.ser.length - 1][2]) : 0) - height / 2
         + (fc.ser.length && Math.abs(fc.yOf(fc.ser[fc.ser.length - 1][2]) - fc.yOf(fc.ser[fc.ser.length - 1][1])) < height ? height : 0)
      font.pixelSize: Style.font.bodySmall; color: root.cMagenta; text: "Fatigue"
    }
    Item {
      id: fcAxis
      y: fcCanvas.y + fc.plotH + Style.space(2)
      width: fc.plotW; height: fcA.implicitHeight
      PText { id: fcA; font.pixelSize: Style.font.bodySmall; color: root.dim; text: fc.ser.length ? root.shortDate(fc.ser[0][0]) : "" }
      PText { anchors.right: parent.right; font.pixelSize: Style.font.bodySmall; color: root.dim; text: "today" }
    }
    Rectangle {
      visible: fc.hoverIdx >= 0
      x: fc.hoverIdx >= 0 ? fc.xOf(fc.hoverIdx) : 0
      y: fcCanvas.y; width: 1; height: fc.plotH
      color: root.foreground; opacity: 0.6
    }
    MouseArea {
      x: 0; y: fcCanvas.y; width: fc.plotW; height: fc.plotH
      hoverEnabled: true
      onPositionChanged: function(m) {
        fc.hoverIdx = fc.ser.length ? Math.max(0, Math.min(fc.ser.length - 1, Math.round(m.x / fc.plotW * (fc.ser.length - 1)))) : -1
      }
      onExited: fc.hoverIdx = -1
    }
    PanelToolTip {
      visible: fc.hoverIdx >= 0
      delay: 0
      x: fc.hoverIdx >= 0 ? Math.min(fc.xOf(fc.hoverIdx) + Style.space(12), fc.width - implicitWidth) : 0
      y: fcCanvas.y - implicitHeight - Style.space(4)
      text: {
        if (fc.hoverIdx < 0) return ""
        var r = fc.ser[fc.hoverIdx]
        return root.shortDate(r[0]) + (r[4] ? "  ·  Relative Effort " + Math.round(r[4]) : "  ·  no activity")
               + "\nFitness " + r[1].toFixed(0) + "  ·  Fatigue " + r[2].toFixed(0) + "  ·  Form " + (r[3] > 0 ? "+" : "") + r[3].toFixed(0)
      }
    }
  }
}
