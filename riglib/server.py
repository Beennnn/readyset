"""Local web dashboard — a global rig-state view with per-item fix/relaunch.

Pure stdlib (http.server). The page auto-refreshes the state (read-only checks);
fixes and the full preflight are explicit POSTs triggered by buttons, and honour
the dry-run toggle end to end. Bind to 127.0.0.1 only — never exposed off-machine.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import checks, launch, midimon, remedy

_MON = midimon.MidiMonitor()   # shared live MIDI monitor for the soundcheck page
_MODE = {"requested": None}    # None → use cfg default; else "auto"|"live"|"studio"
_MANUAL = {"iphone_charge": False}  # manual confirmations (things the Mac can't detect)

_GROUP = {"app:": "Apps", "usb:": "Stream Deck", "kbd:": "Clavier & jeu",
          "net:": "Réseau", "lamp:": "Lampes", "sys:": "Système",
          "midi?:": "MIDI optionnel", "midi:": "MIDI requis", "audio": "Audio"}


def _group_of(key: str) -> str:
    for prefix, name in _GROUP.items():
        if key.startswith(prefix):
            return name
    return "Autre"


def build_state(cfg: dict, with_audio: bool = True) -> dict:
    requested = _MODE["requested"] or cfg.get("mode", {}).get("default", "auto")
    mode = checks.resolve_mode(cfg, requested)
    # checks.run_all caches the slow system_profiler call internally, so polling is cheap.
    results = checks.run_all(cfg, mode, with_audio=with_audio, manual=_MANUAL)
    items = []
    for r in results:
        rem = remedy.resolve(cfg, r)
        items.append({
            **r.to_dict(),
            "group": _group_of(r.key),
            "remedy": rem.label if (rem and r.status != checks.OK) else None,
        })
    status = checks.worst(results)
    return {
        "status": status,
        "mode": mode,
        "requested": requested,
        "fails": sum(1 for r in results if r.status == checks.FAIL),
        "warns": sum(1 for r in results if r.status == checks.WARN),
        "total": len(results),
        "items": items,
    }


class _Handler(BaseHTTPRequestHandler):
    cfg: dict = {}

    def log_message(self, *_):  # silence default request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/soundcheck"):
            self._send(200, SOUNDCHECK.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            self._json(build_state(self.cfg))
        elif self.path.startswith("/api/mode"):
            self._json({"requested": _MODE["requested"] or self.cfg.get("mode", {}).get("default", "auto"),
                        "resolved": checks.resolve_mode(self.cfg, _MODE["requested"] or self.cfg.get("mode", {}).get("default", "auto"))})
        elif self.path.startswith("/api/midi"):
            self._json(_MON.snapshot())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        body = self._read_body()
        dry = bool(body.get("dry", False))
        if self.path == "/api/mode":
            m = body.get("mode", "auto")
            _MODE["requested"] = m if m in ("auto", "live", "studio") else None
            self._json({"ok": True, "requested": _MODE["requested"]})
        elif self.path == "/api/manual":
            key = body.get("key", "")
            if key in _MANUAL:
                _MANUAL[key] = bool(body.get("value", False))
            self._json({"ok": True, key: _MANUAL.get(key)})
        elif self.path == "/api/midi/start":
            _MON.start()
            self._json({"ok": True})
        elif self.path == "/api/midi/stop":
            _MON.stop()
            self._json({"ok": True})
        elif self.path == "/api/midi/audio":
            _MON.confirm_audio(bool(body.get("ok", False)))
            self._json({"ok": True})
        elif self.path == "/api/fix":
            rem = remedy.resolve_key(self.cfg, body.get("key", ""))
            if rem is None:
                self._json({"ok": False, "message": "Aucune action automatique pour cet élément."})
                return
            ok, msg = rem.run(dry)
            self._json({"ok": ok, "message": msg})
        elif self.path == "/api/preflight":
            logs: list[str] = []
            launch.bring_up(self.cfg, log=logs.append, dry_run=dry)
            self._json({"ok": True, "message": "\n".join(logs)})
        else:
            self._json({"ok": False, "message": "route inconnue"}, code=404)


def serve(cfg: dict, port: int = 8765, open_browser: bool = True) -> None:
    handler = type("Handler", (_Handler,), {"cfg": cfg})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard rig → {url}  (Ctrl-C pour arrêter)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard arrêté.")
    finally:
        httpd.server_close()


# --- the page (self-contained: inline CSS + JS, no external requests) ----------
PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🎹 Rig — état</title>
<style>
  :root{color-scheme:dark;--bg:#0d0f14;--card:#161a22;--line:#242a36;--tx:#e7ebf2;--mut:#8a93a3;
        --ok:#2ecc71;--warn:#f4b942;--fail:#ff5468;--accent:#4a9eff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{position:sticky;top:0;background:linear-gradient(#0d0f14,#0d0f14ee);padding:16px 20px;border-bottom:1px solid var(--line);z-index:5}
  h1{margin:0 0 4px;font-size:19px}
  .sub{color:var(--mut);font-size:13px}
  .banner{margin:12px 0 0;padding:12px 16px;border-radius:10px;font-weight:600;display:flex;gap:10px;align-items:center}
  .banner.ok{background:#123322;color:var(--ok)} .banner.warn{background:#332a12;color:var(--warn)} .banner.fail{background:#3a1620;color:var(--fail)}
  .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{border:none;border-radius:0;background:var(--card);padding:8px 14px}
  .seg button.on{background:var(--accent);color:#03122b;font-weight:600}
  .modeinfo{font-size:13px;color:var(--mut)} .modeinfo b{color:var(--tx)}
  button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--tx);padding:8px 14px;border-radius:9px;cursor:pointer}
  button:hover{border-color:var(--accent)} button:active{transform:translateY(1px)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#03122b;font-weight:600}
  button.fix{background:transparent;border-color:var(--fail);color:var(--fail);padding:6px 12px;font-size:13px}
  button.fix:hover{background:var(--fail);color:#2a0009}
  label.dry{display:flex;gap:6px;align-items:center;color:var(--mut);font-size:13px;user-select:none}
  main{padding:8px 20px 40px;max-width:820px;margin:0 auto}
  .grp{margin-top:22px} .grp h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}
  .row{display:flex;align-items:center;gap:11px;background:var(--card);border:1px solid var(--line);border-left-width:4px;border-radius:9px;padding:8px 13px;margin-bottom:6px}
  .row.ok{border-left-color:var(--ok)} .row.warn{border-left-color:var(--warn)} .row.fail{border-left-color:var(--fail)}
  .allok{padding:14px;background:#123322;color:var(--ok);border-radius:10px;font-weight:600;text-align:center}
  #okwrap{margin-top:14px} #okwrap summary{cursor:pointer;color:var(--mut);font-size:12px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px;list-style:none}
  #okwrap summary::-webkit-details-marker{display:none}
  .chip{display:inline-flex;align-items:center;gap:5px;background:#11211a;border:1px solid #1f3a2b;color:#9fe0b8;border-radius:20px;padding:3px 11px;font-size:12px;margin:0 5px 5px 0}
  .chip.warn{background:#231d10;border-color:#3a3016;color:var(--warn)}
  .ic{font-size:20px;width:26px;text-align:center;flex:none}
  .dot{width:12px;height:12px;border-radius:50%;flex:none}
  .dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.fail{background:var(--fail)} .dot.neutral{background:var(--mut)}
  #diagram{overflow-x:auto;margin:14px 0 6px;padding:14px 10px;background:var(--card);border:1px solid var(--line);border-radius:14px}
  #diagram svg{width:100%;min-width:620px;height:auto;display:block}
  .difoot{color:var(--mut);font-size:12px;text-align:center;margin-top:6px}
  .lab{flex:1;min-width:0} .lab .t{font-weight:500} .lab .d{color:var(--mut);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #log{white-space:pre-wrap;background:#0a0c11;border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:18px;font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--mut);max-height:200px;overflow:auto;display:none}
  .stamp{color:var(--mut);font-size:12px}
  @media(max-width:520px){.row{flex-wrap:wrap}.lab .d{white-space:normal}}
</style></head><body>
<header>
  <h1>🎹 Rig — état global</h1>
  <div class="sub">Vue auto-rafraîchie (lecture seule). Les actions sont des clics explicites.</div>
  <div id="banner" class="banner warn">…chargement</div>
  <div class="bar">
    <span class="seg" id="modeseg">
      <button data-m="auto" onclick="setMode('auto')">Auto</button>
      <button data-m="live" onclick="setMode('live')">🎤 Live</button>
      <button data-m="studio" onclick="setMode('studio')">🎧 Studio</button>
    </span>
    <span class="modeinfo" id="modeinfo"></span>
    <button class="primary" onclick="preflight()">▶ Préflight complet</button>
    <button onclick="refresh()">↻ Rafraîchir</button>
    <button onclick="location.href='/soundcheck'">🎹 Soundcheck</button>
    <label class="dry"><input type="checkbox" id="dry"> mode simulation (dry-run)</label>
    <span class="stamp" id="stamp"></span>
  </div>
</header>
<main><div id="diagram"></div><div id="problems"></div>
  <details id="okwrap"><summary id="oksum"></summary><div id="okchips"></div></details>
  <div id="log"></div></main>
<script>
const IC={ok:"✅",warn:"⚠️",fail:"❌"};
const dry=()=>document.getElementById("dry").checked;
async function setMode(m){await fetch("/api/mode",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:m})});refresh();}
async function manualSet(key,val){await fetch("/api/manual",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,value:val})});refresh();}
function iconFor(k){
  if(k.startsWith("app:Ableton"))return"🎵";
  if(k.startsWith("app:Stream"))return"🎛️";
  if(k.startsWith("app:Bome"))return"🔀";
  if(k.startsWith("app:Stage"))return"▶️";
  if(k.startsWith("usb:"))return"🎛️";
  if(k.startsWith("lamp:"))return"💡";
  if(k.startsWith("kbd:breath"))return"🌬️";
  if(k.startsWith("kbd:"))return"🎹";
  if(k.startsWith("net:modem"))return"📡";
  if(k.startsWith("net:stage"))return"🌐";
  if(k.startsWith("net:"))return"📱";
  if(k.startsWith("sys:vpn"))return"🔒";
  if(k.startsWith("sys:output"))return"💻";
  if(k.startsWith("sys:amphetamine"))return"☕";
  if(k.startsWith("sys:macpower"))return"🔌";
  if(k.startsWith("sys:iphonecharge"))return"🔋";
  if(k.startsWith("audio:live"))return"🎚️";
  if(k==="audio")return"🔊";
  if(k.startsWith("midi?:"))return"🎹";
  if(k.startsWith("midi:"))return"🔌";
  return"•";
}
// --- signal-flow diagram: nodes coloured by the checks that feed them ----------
const stc=s=>({ok:"#2ecc71",warn:"#f4b942",fail:"#ff5468"}[s]||"#55607a");
function nstat(keys,map){let s="neutral";for(const k of keys){const v=map[k];
  if(v==="fail")return"fail";if(v==="warn")s="warn";else if(v==="ok"&&s==="neutral")s="ok";}return s;}
const DNODES=[
  {e:"📱",l:"iPhone",k:["net:iphone"],x:88,y:56},
  {e:"🎛️",l:"Stream Deck",k:["app:Stream Deck"],x:88,y:150},
  {e:"🎹",l:"Clavier",k:["kbd:keyboard"],x:88,y:244},
  {e:"🔀",l:"Bome",k:["app:Bome MIDI Translator","app:Bome Network"],x:312,y:103},
  {e:"▶️",l:"Stage Traxx",k:["app:Stage Traxx"],x:312,y:244},
  {e:"🎵",l:"Ableton Live",k:["app:Ableton","midi:Ableton Loopback"],x:520,y:150},
  {e:"🔊",l:"Sortie audio",k:["audio","audio:live"],x:684,y:150},
];
const DEDGES=[[0,3],[1,3],[2,5],[3,5],[4,5],[5,6]];
function renderDiagram(items){
  const map={};for(const it of items)map[it.key]=it.status;
  const N=DNODES.map(n=>({...n,s:nstat(n.k,map)}));
  let e="";
  for(const [a,b] of DEDGES){const A=N[a],B=N[b];
    const dx=B.x-A.x,dy=B.y-A.y,d=Math.hypot(dx,dy),ux=dx/d,uy=dy/d;
    const x1=A.x+ux*76,y1=A.y+uy*30,x2=B.x-ux*80,y2=B.y-uy*30;
    e+=`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#3a4356" stroke-width="2" marker-end="url(#ar)"/>`;}
  let g="";
  for(const n of N){const c=stc(n.s);
    g+=`<g><rect x="${n.x-72}" y="${n.y-25}" width="144" height="50" rx="12" fill="#12161e" stroke="${c}" stroke-width="2"/>`
      +`<text x="${n.x-56}" y="${n.y+7}" font-size="21">${n.e}</text>`
      +`<text x="${n.x-28}" y="${n.y+5}" fill="#e7ebf2" font-size="12.5" font-weight="600">${n.l}</text>`
      +`<circle cx="${n.x+58}" cy="${n.y-14}" r="4.5" fill="${c}"/></g>`;}
  document.getElementById("diagram").innerHTML=
    `<svg viewBox="0 0 764 300" xmlns="http://www.w3.org/2000/svg">`
    +`<defs><marker id="ar" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">`
    +`<path d="M0,0 L7,3 L0,6 Z" fill="#3a4356"/></marker></defs>${e}${g}</svg>`
    +`<div class="difoot">Flux du rig — chaque bloc vire au vert/orange/rouge selon ses checks.</div>`;
}
function logline(t){const l=document.getElementById("log");l.style.display="block";l.textContent=(new Date().toLocaleTimeString()+"  "+t+"\n"+l.textContent).slice(0,4000);}
async function refresh(){
  const y=window.scrollY;
  let s;try{s=await(await fetch("/api/state")).json();}catch(e){return;}
  const b=document.getElementById("banner");b.className="banner "+s.status;
  b.textContent=s.status==="ok"?"✅ Rig prêt — tout est vert."
    :s.status==="warn"?`⚠️ Rig jouable — ${s.warns} avertissement(s).`
    :`❌ Rig PAS prêt — ${s.fails} bloquant(s), ${s.warns} avertissement(s).`;
  document.querySelectorAll("#modeseg button").forEach(b=>b.classList.toggle("on",b.dataset.m===s.requested));
  document.getElementById("modeinfo").innerHTML=`mode : <b>${s.mode==="live"?"🎤 Live":"🎧 Studio"}</b>${s.requested==="auto"?" (auto)":""}`;
  renderDiagram(s.items);
  // Problems first (fail then warn) as full rows; everything OK collapses to chips.
  const bad=s.items.filter(it=>it.status!=="ok").sort((a,b)=>(a.status==="fail"?0:1)-(b.status==="fail"?0:1));
  const good=s.items.filter(it=>it.status==="ok");
  const prob=document.getElementById("problems");
  if(!bad.length){prob.innerHTML='<div class="allok">✅ Tout est vert — rien à corriger.</div>';}
  else{prob.innerHTML="";for(const it of bad){
    const row=document.createElement("div");row.className="row "+it.status;
    row.innerHTML=`<div class="ic">${iconFor(it.key)}</div>
      <div class="lab"><div class="t">${it.label}</div>${it.detail?`<div class="d">${it.detail}</div>`:""}</div>
      <div class="dot ${it.status}"></div>`;
    if(it.remedy){const btn=document.createElement("button");btn.className="fix";btn.textContent=it.remedy;
      btn.onclick=()=>fix(it.key,it.remedy);row.appendChild(btn);}
    if(it.key==="sys:iphonecharge"){const b=document.createElement("button");b.className="fix";
      b.textContent="✓ Confirmer en charge";b.onclick=()=>manualSet("iphone_charge",true);row.appendChild(b);}
    prob.appendChild(row);
  }}
  document.getElementById("oksum").textContent=good.length?`▸ ${good.length} checks OK (déplier)`:"";
  const okc=document.getElementById("okchips");okc.innerHTML="";
  for(const it of good){const c=document.createElement("span");c.className="chip";
    c.innerHTML=`${iconFor(it.key)} ${it.label}`;c.title=it.detail||"";
    if(it.key==="sys:iphonecharge"){c.style.cursor="pointer";c.title="cliquer pour réinitialiser";
      c.onclick=()=>manualSet("iphone_charge",false);}
    okc.appendChild(c);}
  document.getElementById("stamp").textContent="maj "+new Date().toLocaleTimeString();
  window.scrollTo(0,y);
}
async function fix(key,label){
  logline(`→ ${label}${dry()?" (dry-run)":""}…`);
  const r=await(await fetch("/api/fix",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({key,dry:dry()})})).json();
  logline((r.ok?"✔ ":"✖ ")+(r.message||"").replace(/\n/g,"  |  "));
  setTimeout(refresh,800);
}
async function preflight(){
  logline(`▶ préflight${dry()?" (dry-run)":""}…`);
  const r=await(await fetch("/api/preflight",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({dry:dry()})})).json();
  logline((r.message||"").replace(/\n/g,"  |  "));
  setTimeout(refresh,1200);
}
refresh();setInterval(refresh,4000);
</script></body></html>"""


# --- soundcheck / play-test page ----------------------------------------------
SOUNDCHECK = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🎹 Soundcheck</title>
<style>
  :root{color-scheme:dark;--bg:#0d0f14;--card:#161a22;--line:#242a36;--tx:#e7ebf2;--mut:#8a93a3;
        --ok:#2ecc71;--warn:#f4b942;--fail:#ff5468;--accent:#4a9eff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--tx);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{padding:16px 20px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:19px} .sub{color:var(--mut);font-size:13px;margin-top:4px}
  a.back{color:var(--accent);text-decoration:none;font-size:13px}
  main{max-width:820px;margin:0 auto;padding:16px 20px 40px}
  .bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
  button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--tx);padding:9px 15px;border-radius:9px;cursor:pointer}
  button.primary{background:var(--accent);border-color:var(--accent);color:#03122b;font-weight:600}
  button:active{transform:translateY(1px)}
  .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;text-align:center;transition:.2s}
  .tile.hit{border-color:var(--ok);background:#123322}
  .tile .e{font-size:26px} .tile .t{font-weight:600;margin-top:4px} .tile .d{color:var(--mut);font-size:12px;margin-top:2px;min-height:16px}
  .tile.hit .d{color:var(--ok)}
  h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:22px 0 8px}
  .prow{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);border-left:4px solid var(--ok);border-radius:10px;padding:9px 13px;margin-bottom:7px}
  .prow .n{flex:1;font-weight:500} .prow .c{color:var(--mut);font-size:12px} .prow .l{color:var(--accent);font-size:12px;font-family:ui-monospace,Menlo,monospace}
  .empty{color:var(--mut);font-size:13px;padding:8px 2px}
  .smgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
  .smpanel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
  .smhead{font-weight:600;font-size:13px;margin-bottom:8px} .smhead .ch{color:var(--accent)}
  .notes{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px;min-height:4px}
  .note{background:#123322;border:1px solid var(--ok);color:var(--ok);border-radius:6px;padding:2px 7px;font-size:12px;font-family:ui-monospace,Menlo,monospace}
  .ccrow{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
  .ccname{width:62px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ccbar{flex:1;height:8px;background:#0a0c11;border-radius:4px;overflow:hidden}
  .ccfill{height:100%;background:var(--accent)} .ccval{width:30px;text-align:right;font-family:ui-monospace,Menlo,monospace}
  .smmeta{color:var(--mut);font-size:12px;margin-top:6px}
  #log{white-space:pre-wrap;background:#0a0c11;border:1px solid var(--line);border-radius:10px;padding:12px;
       font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--mut);max-height:180px;overflow:auto}
  .audio{margin-top:20px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .audio.y{border-color:var(--ok)} .audio.n{border-color:var(--fail)}
  .wizard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:20px}
  .wstep{display:flex;align-items:center;gap:10px;padding:9px 6px;border-bottom:1px solid var(--line)}
  .wstep:last-child{border-bottom:none}
  .wstep.wcur{background:#10233f;border-radius:8px;padding:11px 10px}
  .wic{width:24px;text-align:center;font-size:16px}
  .wtxt{flex:1} .wstep.wcur .wtxt{font-weight:600} .wstep.wdone .wtxt{color:var(--mut)}
  .winfo{color:var(--accent);font-size:12px;font-family:ui-monospace,Menlo,monospace;margin-left:6px}
  .wdonemsg{margin-top:12px;padding:12px;background:#123322;color:var(--ok);border-radius:8px;font-weight:600;text-align:center}
  @media(max-width:520px){.tiles{grid-template-columns:1fr}}
</style></head><body>
<header>
  <a class="back" href="/">← retour à l'état du rig</a>
  <h1 style="margin-top:6px">🎹 Soundcheck — test de jeu <span id="scmode" class="sub"></span></h1>
  <div class="sub">Démarre, puis : 1) appuie sur la pédale · 2) joue des notes · 3) souffle dans le breath · 4) bouge tes contrôleurs. Les cases s'allument en direct.</div>
</header>
<main>
  <h2 style="margin-top:0">🎯 Test guidé — je te demande de faire, je vérifie</h2>
  <div class="wizard">
    <div class="bar">
      <button class="primary" id="wizbtn" onclick="wizStart()">▶ Lancer le test guidé</button>
      <button onclick="wizReset()">↺ Recommencer</button>
    </div>
    <div id="wizsteps"><div class="empty">Clique pour démarrer — je te guide étape par étape.</div></div>
  </div>
  <h2>🎛️ Écoute libre</h2>
  <div class="bar">
    <button class="primary" id="startbtn" onclick="toggle()">▶ Démarrer l'écoute MIDI</button>
    <span class="sub" id="status">arrêté</span>
  </div>
  <div class="tiles">
    <div class="tile" id="t-pedal"><div class="e">🦶</div><div class="t">Pédale (CC64)</div><div class="d" id="d-pedal">—</div></div>
    <div class="tile" id="t-notes"><div class="e">🎵</div><div class="t">Notes MIDI</div><div class="d" id="d-notes">—</div></div>
    <div class="tile" id="t-breath"><div class="e">🌬️</div><div class="t">Breath</div><div class="d" id="d-breath">—</div></div>
  </div>
  <h2>Contrôleurs actifs (chaque source qui envoie du MIDI apparaît ici)</h2>
  <div id="ports"><div class="empty">Aucune activité pour l'instant — démarre puis joue.</div></div>
  <h2>Vue MIDI en direct — façon ShowMIDI (par canal : notes tenues, CC, pitch)</h2>
  <div id="showmidi"><div class="empty">Rien reçu — démarre l'écoute puis joue.</div></div>
  <h2>Flux brut</h2>
  <div id="log">—</div>
  <div class="audio" id="audiobox">
    <b>🔊 Son sur <span id="audiotarget">le P-225</span> ?</b> — le logiciel ne peut pas l'entendre, confirme toi-même :
    <div style="margin-top:8px;display:flex;gap:10px">
      <button onclick="audio(true)">✅ J'entends le son</button>
      <button onclick="audio(false)">❌ Pas de son</button>
      <span class="sub" id="audiostate" style="align-self:center"></span>
    </div>
  </div>
</main>
<script>
let running=false;
let scMode="live";
async function fetchMode(){
  try{const m=await(await fetch("/api/mode")).json();scMode=m.resolved;}catch(e){}
  document.getElementById("scmode").textContent=scMode==="studio"?"— 🎧 Studio":"— 🎤 Live";
  document.getElementById("audiotarget").textContent=scMode==="studio"?"macOS / RME":"le P-225";
}
const NN=["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
function noteName(n){return NN[n%12]+(Math.floor(n/12)-1);}
function renderShowMidi(chs){
  const sm=document.getElementById("showmidi");
  if(!chs||!chs.length){sm.innerHTML='<div class="empty">Rien reçu — démarre l\'écoute puis joue.</div>';return;}
  sm.innerHTML='<div class="smgrid"></div>';const grid=sm.firstChild;
  for(const c of chs){
    const notes=c.notes.map(x=>`<span class="note">${noteName(x.n)} <small>${x.v}</small></span>`).join("");
    const ccs=c.cc.map(x=>`<div class="ccrow"><span class="ccname">${x.name||("CC"+x.n)}</span>
      <span class="ccbar"><span class="ccfill" style="width:${Math.round(x.v/127*100)}%"></span></span>
      <span class="ccval">${x.v}</span></div>`).join("");
    let meta=[];if(c.pitch!=null&&c.pitch!==0)meta.push("pitch "+c.pitch);
    if(c.prog!=null)meta.push("prog "+c.prog);if(c.at!=null&&c.at!==0)meta.push("aftertouch "+c.at);
    const p=document.createElement("div");p.className="smpanel";
    p.innerHTML=`<div class="smhead">${c.port} · <span class="ch">Ch ${c.ch}</span></div>
      <div class="notes">${notes||'<span class="smmeta">aucune note tenue</span>'}</div>
      ${ccs}${meta.length?`<div class="smmeta">${meta.join(" · ")}</div>`:""}`;
    grid.appendChild(p);
  }
}
// --- guided test (wizard) -----------------------------------------------------
let wiz={active:false,done:{},skip:{}};
function wizStart(){wiz={active:true,done:{},skip:{}};fetch("/api/midi/start",{method:"POST"});document.getElementById("wizbtn").textContent="⏹ Test en cours…";}
function wizReset(){wiz={active:false,done:{},skip:{}};document.getElementById("wizbtn").textContent="▶ Lancer le test guidé";document.getElementById("wizsteps").innerHTML='<div class="empty">Clique pour démarrer — je te guide étape par étape.</div>';}
function wizSkip(id){wiz.skip[id]=true;}
function wizAudio(ok){fetch("/api/midi/audio",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ok})});if(ok)wiz.done.audio=true;else wiz.skip.audio=true;}
function wizSteps(s){
  const ports=(s&&s.ports)||[],active=new Set(ports.filter(p=>p.count>0).map(p=>p.name)),watched=(s&&s.watched)||[];
  return [
    {id:"pedal",txt:"Appuie sur la pédale de sustain",ok:x=>!!(x.flags&&x.flags.pedal_cc64),info:x=>x.flags&&x.flags.pedal_cc64?`✔ CC64=${x.flags.pedal_cc64.value}`:""},
    {id:"notes",txt:"Joue quelques notes au clavier",ok:x=>x.flags&&x.flags.notes>0,info:x=>x.flags&&x.flags.notes>0?`✔ ${x.flags.notes} notes`:""},
    {id:"breath",txt:"Souffle dans le breath controller"+(scMode==="studio"?" (optionnel)":""),skippable:true,ok:x=>!!(x.flags&&x.flags.breath),info:x=>x.flags&&x.flags.breath?"✔ souffle reçu":""},
    {id:"ctrls",txt:"Actionne chacun de tes contrôleurs",skippable:true,ok:_=>watched.length>0&&watched.every(w=>active.has(w)),info:_=>`${[...active].filter(a=>watched.includes(a)).length}/${watched.length} contrôleurs actifs`},
    {id:"audio",txt:`Entends-tu le son ${scMode==="studio"?"(macOS / RME)":"sur le P-225"} ?`,manual:true,ok:x=>x.audio_ok===true,info:x=>x.audio_ok===true?"✔ confirmé":""},
  ];
}
function renderWiz(s){
  if(!wiz.active)return;
  const steps=wizSteps(s||{}),box=document.getElementById("wizsteps");
  for(const st of steps){if(!wiz.done[st.id]&&!wiz.skip[st.id]&&s&&st.ok(s))wiz.done[st.id]=true;}
  const cur=steps.findIndex(st=>!wiz.done[st.id]&&!wiz.skip[st.id]);
  let html="";
  steps.forEach((st,i)=>{
    const done=wiz.done[st.id],sk=wiz.skip[st.id],isCur=(i===cur);
    const ic=done?"✅":sk?"⏭":isCur?"👉":"⚪";
    html+=`<div class="wstep ${done?"wdone":isCur?"wcur":""}"><span class="wic">${ic}</span>
      <span class="wtxt">${st.txt}<span class="winfo">${s?(st.info(s)||""):""}</span></span>`;
    if(isCur&&st.manual)html+=`<span><button onclick="wizAudio(true)">✅ Oui</button> <button onclick="wizAudio(false)">❌ Non</button></span>`;
    else if(isCur&&st.skippable)html+=`<button class="fix" onclick="wizSkip('${st.id}')">Passer</button>`;
    html+=`</div>`;
  });
  if(cur===-1){const okAll=steps.every(st=>wiz.done[st.id]);
    html+=`<div class="wdonemsg">${okAll?"✅ Test réussi — tout est vérifié !":"⚠️ Test terminé (des étapes passées/non confirmées)"}</div>`;}
  box.innerHTML=html;
}
async function toggle(){
  running=!running;
  await fetch(running?"/api/midi/start":"/api/midi/stop",{method:"POST"});
  document.getElementById("startbtn").textContent=running?"⏹ Arrêter":"▶ Démarrer l'écoute MIDI";
}
async function audio(ok){
  await fetch("/api/midi/audio",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ok})});
}
function tile(id,hit,detail){const el=document.getElementById("t-"+id);el.classList.toggle("hit",hit);
  document.getElementById("d-"+id).textContent=detail;}
async function poll(){
  let s;try{s=await(await fetch("/api/midi")).json();}catch(e){return;}
  document.getElementById("status").textContent=s.running?`● écoute — ${(s.watched||[]).length} ports physiques (${(s.watched||[]).join(", ")||"—"})`:"arrêté";
  running=s.running;document.getElementById("startbtn").textContent=s.running?"⏹ Arrêter":"▶ Démarrer l'écoute MIDI";
  renderWiz(s);
  const f=s.flags||{};
  tile("pedal",!!f.pedal_cc64,f.pedal_cc64?`CC64=${f.pedal_cc64.value} — ${f.pedal_cc64.port}`:"pas encore vue");
  tile("notes",f.notes>0,f.notes>0?`${f.notes} note(s) reçue(s)`:"pas encore de note");
  tile("breath",!!f.breath,f.breath?`OK — ${f.breath.port}`:"pas encore de souffle");
  const host=document.getElementById("ports");
  if(!s.ports||!s.ports.length){host.innerHTML='<div class="empty">Aucune activité pour l\'instant.</div>';}
  else{host.innerHTML="";for(const p of s.ports){const d=document.createElement("div");d.className="prow";
    d.innerHTML=`<div class="n">${p.name}</div><div class="l">${p.last||""}</div><div class="c">${p.count} msg</div>`;host.appendChild(d);}}
  renderShowMidi(s.channels);
  document.getElementById("log").textContent=(s.events||[]).map(e=>`${e.port.padEnd(22).slice(0,22)}  ${e.msg}`).join("\n")||"—";
  const ab=document.getElementById("audiobox"),as=document.getElementById("audiostate");
  ab.className="audio"+(s.audio_ok===true?" y":s.audio_ok===false?" n":"");
  as.textContent=s.audio_ok===true?"✅ confirmé":s.audio_ok===false?"❌ pas de son":"";
}
fetchMode();setInterval(fetchMode,5000);
poll();setInterval(poll,300);
</script></body></html>"""
