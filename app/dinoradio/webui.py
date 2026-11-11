"""The radio's web page: http://<the Pi>:8080 - live status, a player for the stream, and the log."""
import collections
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("web")


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines for the web page."""

    def __init__(self, size=600):
        super().__init__()
        self.lines = collections.deque(maxlen=size)
        self.seq = 0
        self.buf_lock = threading.Lock()

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with self.buf_lock:
            self.seq += 1
            self.lines.append({"n": self.seq, "t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                               "level": record.levelname, "src": record.name, "msg": msg})

    def since(self, n):
        with self.buf_lock:
            return [l for l in self.lines if l["n"] > n]


LOG = LogBuffer()


def serve(brain, port: int):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self._send(PAGE, "text/html; charset=utf-8")
            elif url.path == "/api/status":
                st = brain.status()
                st["stream_port"] = brain.s.icecast_port
                st["stream_mount"] = brain.s.stream_mount
                self._send(json.dumps(st), "application/json")
            elif url.path == "/api/log":
                after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
                self._send(json.dumps(LOG.since(after)), "application/json")
            else:
                self._send("Not found", "text/plain", 404)

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        server.daemon_threads = True
        log.info("web page on port %d", port)
        server.serve_forever()
    except OSError as e:
        log.warning("web page couldn't start on port %d: %s", port, e)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dino's Radio</title>
<style>
  :root {
    --bg: #0d0f1f; --panel: #161a33; --panel2: #1d2242; --line: #2a3060;
    --text: #eef0ff; --muted: #9aa0c0; --pink: #e84fa6; --blue: #3fa9f5; --violet: #8a5cf6;
    --amber: #f5a524; --green: #3ddc84; --red: #ff5a5f;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  body { display: flex; flex-direction: column; }
  .top { flex: 3 1 0; overflow-y: auto; padding: 18px 22px 12px; min-height: 0; }
  .bottom { flex: 1 1 0; min-height: 140px; display: flex; flex-direction: column;
    border-top: 1px solid var(--line); background: #080a16; }
  header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
  h1 { margin: 0; font-size: 26px; letter-spacing: .02em;
    background: linear-gradient(90deg, var(--blue), var(--violet), var(--pink));
    -webkit-background-clip: text; background-clip: text; color: transparent; }
  .badge { font-weight: 700; font-size: 12px; letter-spacing: .12em; padding: 4px 10px; border-radius: 99px; }
  .onair { background: rgba(61,220,132,.15); color: var(--green); border: 1px solid var(--green); }
  .standby { background: rgba(245,165,36,.15); color: var(--amber); border: 1px solid var(--amber); }
  .off { background: rgba(255,90,95,.15); color: var(--red); border: 1px solid var(--red); }
  .grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 16px; }
  @media (max-width: 820px) { .grid { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; }
  .label { font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
  .now-title { font-size: 26px; font-weight: 700; line-height: 1.2; }
  .now-artist { font-size: 18px; color: var(--muted); margin-top: 2px; }
  .chip { display: inline-block; font-size: 12px; font-weight: 700; padding: 2px 8px; border-radius: 6px;
    background: rgba(245,165,36,.16); color: var(--amber); margin-left: 6px; vertical-align: middle; }
  .dj { margin-top: 10px; font-style: italic; color: #cfd3ff; border-left: 3px solid var(--violet); padding-left: 10px; }
  .next { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
  .next .t { font-weight: 600; }
  .player { display: flex; align-items: center; gap: 14px; margin-top: 16px; }
  button { font: inherit; font-weight: 700; border: 0; border-radius: 99px; padding: 10px 20px; cursor: pointer;
    color: white; background: linear-gradient(90deg, var(--violet), var(--pink)); }
  button:focus-visible { outline: 3px solid var(--blue); outline-offset: 2px; }
  input[type=range] { width: 120px; accent-color: var(--pink); }
  .muted { color: var(--muted); font-size: 13px; }
  .stats { display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; font-size: 14px; }
  .stats div:nth-child(odd) { color: var(--muted); white-space: nowrap; }
  .bar { height: 6px; background: var(--panel2); border-radius: 99px; overflow: hidden; margin-top: 4px; }
  .bar i { display: block; height: 100%; background: linear-gradient(90deg, var(--blue), var(--violet)); width: 0; }
  ol { margin: 0; padding-left: 0; list-style: none; }
  ol li { display: flex; gap: 10px; padding: 4px 0; border-bottom: 1px solid #20254a; font-size: 14px; }
  ol li .when { color: var(--muted); font-variant-numeric: tabular-nums; }
  ol li .sb { color: var(--amber); font-size: 12px; }
  .loghead { display: flex; justify-content: space-between; align-items: center; padding: 6px 14px;
    font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid #1a1e3a; }
  .loghead label { letter-spacing: normal; text-transform: none; font-size: 12px; }
  #log { flex: 1; overflow-y: auto; padding: 6px 14px 10px; font: 12.5px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace; }
  #log .l { white-space: pre-wrap; word-break: break-word; }
  #log .t { color: #6b7199; }
  #log .WARNING { color: var(--amber); } #log .ERROR, #log .CRITICAL { color: var(--red); }
  #log .src { color: var(--blue); }
  .err { color: var(--red); }
</style>
</head>
<body>
<div class="top">
  <header>
    <h1 id="station">Dino's Radio</h1>
    <span id="state" class="badge off">CONNECTING</span>
    <span class="muted" id="updated"></span>
  </header>
  <div class="grid">
    <div>
      <div class="card">
        <div class="label">Now playing</div>
        <div class="now-title" id="nowTitle">—</div>
        <div class="now-artist" id="nowArtist"></div>
        <div class="dj" id="nowDj" hidden></div>
        <div class="next" id="nextBox" hidden>
          <div class="label">Up next</div>
          <div><span class="t" id="nextTitle"></span> <span class="muted" id="nextArtist"></span><span id="nextChart"></span></div>
          <div class="dj" id="nextDj" hidden></div>
        </div>
        <div class="player">
          <button id="play" aria-label="Listen to the stream">▶ Listen</button>
          <label class="muted">Volume <input id="vol" type="range" min="0" max="1" step="0.05" value="0.8"></label>
          <span class="muted" id="pstate"></span>
        </div>
        <audio id="audio" preload="none"></audio>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="label">Recently played</div>
        <ol id="history"></ol>
      </div>
    </div>
    <div class="card">
      <div class="label">Station</div>
      <div class="stats">
        <div>NAS</div><div id="nas"></div>
        <div>Library</div><div id="library"></div>
        <div>Chart positions</div><div><span id="charts"></span><div class="bar"><i id="chartsBar"></i></div></div>
        <div>Levelling</div><div><span id="levelling"></span><div class="bar"><i id="levelBar"></i></div></div>
        <div>DJ</div><div id="dj"></div>
        <div>Standby</div><div id="standby"></div>
        <div>Backup</div><div id="backup"></div>
        <div>Up for</div><div id="uptime"></div>
        <div>Stream</div><div id="stream" style="word-break:break-all"></div>
      </div>
    </div>
  </div>
</div>
<div class="bottom">
  <div class="loghead"><span>Log</span><label><input type="checkbox" id="follow" checked> Follow</label></div>
  <div id="log" aria-live="off"></div>
</div>
<script>
const $ = id => document.getElementById(id);
const audio = $("audio");
let streamUrl = "";
let playing = false;

function fmtMinutes(m) {
  if (m == null) return "";
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  return (d ? d + "d " : "") + (h ? h + "h " : "") + mm + "m";
}
function chip(text) { return text ? ' <span class="chip">' + esc(text) + '</span>' : ""; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

async function refresh() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    const s = await r.json();
    $("station").textContent = s.station;
    document.title = s.station;
    const st = $("state");
    st.textContent = s.state + (s.state === "STANDBY" && s.standby_minutes != null ? " · " + s.standby_minutes + " min" : "");
    st.className = "badge " + (s.state === "ON AIR" ? "onair" : s.state === "STANDBY" ? "standby" : "off");
    const now = s.now || {};
    $("nowTitle").innerHTML = esc(now.title || "—") + chip(now.chart);
    $("nowArtist").textContent = now.kind === "standby" ? "Standby track" : (now.artist || "");
    $("nowDj").hidden = !now.dj || now.dj === "(standby message)";
    $("nowDj").textContent = now.dj ? "“" + now.dj + "”" : "";
    const nx = s.next;
    $("nextBox").hidden = !nx;
    if (nx) {
      $("nextTitle").textContent = nx.title || "";
      $("nextArtist").textContent = nx.kind === "standby" ? "· standby" : "· " + (nx.artist || "");
      $("nextChart").innerHTML = chip(nx.chart);
      $("nextDj").hidden = !nx.dj || nx.dj === "(standby message)";
      $("nextDj").textContent = nx.dj ? "“" + nx.dj + "”" : "";
    }
    $("nas").innerHTML = s.nas === "Connected" ? esc(s.nas) : '<span class="err">' + esc(s.nas) + '</span>';
    $("library").textContent = s.library;
    const c = s.charts, l = s.levelling;
    $("charts").textContent = c.checked + " of " + c.songs + " songs · " + c.top10 + " top 10 · " + c.hits + " hits — " + c.message;
    $("chartsBar").style.width = (c.songs ? 100 * c.checked / c.songs : 0) + "%";
    $("levelling").textContent = l.levelled + " of " + l.total + " tracks";
    $("levelBar").style.width = (l.total ? 100 * l.levelled / l.total : 0) + "%";
    $("dj").textContent = s.dj;
    $("standby").textContent = s.standby_tracks + " tracks, " + s.standby_messages + " messages";
    $("backup").textContent = s.backup || "—";
    $("uptime").textContent = fmtMinutes(s.uptime_minutes);
    streamUrl = location.protocol + "//" + location.hostname + ":" + s.stream_port + "/" + s.stream_mount;
    $("stream").innerHTML = '<a style="color:var(--blue)" href="' + esc(streamUrl) + '">' + esc(streamUrl) + '</a>';
    $("history").innerHTML = (s.history || []).map(h =>
      '<li><span class="when">' + esc(h[0]) + '</span><span>' + esc(h[1]) + ' – ' + esc(h[2]) + '</span>' +
      (h[3] === "standby" ? ' <span class="sb">standby</span>' : '') + '</li>').join("");
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("state").textContent = "CAN'T REACH THE RADIO";
    $("state").className = "badge off";
  }
}

let lastLog = 0;
async function refreshLog() {
  try {
    const r = await fetch("/api/log?after=" + lastLog, {cache: "no-store"});
    const lines = await r.json();
    if (!lines.length) return;
    const box = $("log");
    const frag = document.createDocumentFragment();
    for (const l of lines) {
      lastLog = l.n;
      const div = document.createElement("div");
      div.className = "l " + l.level;
      div.innerHTML = '<span class="t">' + esc(l.t) + '</span> <span class="src">' + esc(l.src) + '</span> ' + esc(l.msg);
      frag.appendChild(div);
    }
    box.appendChild(frag);
    while (box.children.length > 600) box.removeChild(box.firstChild);
    if ($("follow").checked) box.scrollTop = box.scrollHeight;
  } catch (e) {}
}

$("play").onclick = () => {
  if (playing) {
    audio.pause(); audio.removeAttribute("src"); audio.load();
    playing = false; $("play").textContent = "▶ Listen"; $("pstate").textContent = "";
  } else if (streamUrl) {
    audio.src = streamUrl + "?t=" + Date.now();      // always the live point, not an old buffer
    audio.volume = $("vol").value;
    audio.play().then(() => { playing = true; $("play").textContent = "■ Stop"; })
      .catch(e => { $("pstate").textContent = "Couldn't play: " + e.message; });
  }
};
$("vol").oninput = () => { audio.volume = $("vol").value; };
audio.addEventListener("waiting", () => { if (playing) $("pstate").textContent = "Buffering…"; });
audio.addEventListener("playing", () => { $("pstate").textContent = "Live"; });
audio.addEventListener("error", () => {
  if (!playing) return;
  $("pstate").textContent = "Reconnecting…";
  setTimeout(() => { if (playing) { audio.src = streamUrl + "?t=" + Date.now(); audio.play().catch(() => {}); } }, 3000);
});

refresh(); refreshLog();
setInterval(refresh, 2000);
setInterval(refreshLog, 2000);
</script>
</body>
</html>
"""
