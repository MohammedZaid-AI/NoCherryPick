"""Dashboards. python app.py -> http://127.0.0.1:5000

  /        live demo mode -- the engine running against a virtual clock
  /batch   the one-shot batch report over data/*.csv

Every line either page draws comes from a real engine call. There is no
recorded animation and no demo data: the browser only adds the delay between
drawing one event and the next.
"""
import json
import queue

from flask import Flask, Response, jsonify, request

import live
from engine import ROOT, reconcile
from report import score, to_json, truth

app = Flask(__name__)


@app.get("/")
def index_live():
    return (ROOT / "live_page.html").read_text(encoding="utf-8")


@app.get("/stream")
def stream():
    """Server-sent events: one JSON object per engine event, in order."""
    def frame(obj):
        return "data: " + json.dumps(obj) + "\n\n"

    def gen():
        q = live.BUS.subscribe()
        try:
            yield frame(live.WORLD.snapshot())   # so a reload rebuilds the screen
            while True:
                try:
                    yield frame(q.get(timeout=15))
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            live.BUS.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Connection": "keep-alive"})


@app.post("/inject/<name>")
def inject(name):
    try:
        return jsonify(ok=True, detail=live.scenario(name))
    except KeyError:
        return jsonify(ok=False, detail="no such scenario"), 404


@app.post("/speed")
def speed():
    live.WORLD.clock.speed = max(0.0, float(request.json.get("speed", 1)))
    return jsonify(ok=True, speed=live.WORLD.clock.speed)


@app.get("/api/run")
def api_run():
    run = reconcile()
    payload = to_json(run, score(run, truth()))
    return Response(json.dumps(payload), mimetype="application/json")


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Reconciliation</title>
<style>
 :root{--bg:#0e1116;--fg:#e6e9ef;--dim:#8b94a7;--line:#222836;--card:#151a23;
       --ok:#35d07f;--warn:#f5b942;--bad:#ff5d5d}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}
 header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
        align-items:baseline;gap:18px;flex-wrap:wrap}
 h1{font-size:15px;margin:0;letter-spacing:.02em}
 .sub{color:var(--dim)}
 .counters{display:flex;gap:26px;padding:12px 20px;border-bottom:1px solid var(--line)}
 .c b{display:block;font-size:20px;font-weight:600}
 .c span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
 main{display:grid;grid-template-columns:1fr 380px;gap:0;height:calc(100vh - 118px)}
 #stage{position:relative;overflow:auto;padding:16px 20px}
 #grid{position:relative;display:grid;grid-template-columns:1fr 200px 1fr;gap:0}
 #svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
 .col h2{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;
         margin:0 0 8px}
 .row{height:22px;line-height:22px;padding:0 8px;margin-bottom:2px;border-radius:3px;
      background:var(--card);opacity:.35;transition:opacity .2s,background .2s;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .row.on{opacity:1}
 .row.ok{background:#13291f}
 .row.warn{background:#2b2413}
 .row.open{background:#2a1618;opacity:1}
 .row .amt{float:right;color:var(--dim)}
 aside{border-left:1px solid var(--line);overflow:auto;padding:16px}
 aside h2{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;
          margin:0 0 10px}
 .exc{padding:7px 9px;margin-bottom:5px;background:var(--card);border-radius:4px;
      cursor:pointer;border-left:3px solid var(--bad)}
 .exc.soft{border-left-color:var(--warn)}
 .exc.info{border-left-color:var(--dim)}
 .exc:hover{background:#1c222d}
 .exc .code{font-size:11px;letter-spacing:.04em}
 .exc .meta{color:var(--dim);font-size:11px}
 #detail{position:fixed;right:16px;bottom:16px;width:520px;max-height:52vh;overflow:auto;
         background:#111722;border:1px solid var(--line);border-radius:6px;padding:14px;
         display:none;box-shadow:0 12px 40px #0009}
 #detail h3{margin:0 0 8px;font-size:13px}
 #detail p{margin:0 0 9px}
 #detail .k{color:var(--dim);text-transform:uppercase;font-size:10px;letter-spacing:.08em}
 #detail button{position:absolute;top:10px;right:10px;background:none;border:0;
                color:var(--dim);cursor:pointer;font-size:14px}
 .sig{color:var(--dim)}
</style>
<header>
  <h1>Reconciliation &amp; fee verification</h1>
  <span class="sub" id="hdr">loading the real run…</span>
</header>
<div class="counters">
  <div class="c"><b id="cMatched">0</b><span>matched</span></div>
  <div class="c"><b id="cFlagged" style="color:var(--warn)">0</b><span>demoted by self-check</span></div>
  <div class="c"><b id="cExc" style="color:var(--bad)">0</b><span>exceptions</span></div>
  <div class="c"><b id="cRisk" style="color:var(--bad)">₹0</b><span>money at risk</span></div>
  <div class="c"><b id="cFee">₹0</b><span>fee leakage</span></div>
  <div class="c"><b id="cTime">–</b><span>engine time</span></div>
</div>
<main>
  <div id="stage"><div id="grid">
    <svg id="svg"></svg>
    <div class="col" id="orders"><h2>orders</h2></div>
    <div></div>
    <div class="col" id="setts"><h2>bank settlement lines</h2></div>
  </div></div>
  <aside><h2>exceptions — ranked by money at risk × age</h2><div id="excs"></div></aside>
</main>
<div id="detail"><button onclick="detail.style.display='none'">✕</button><div id="dbody"></div></div>
<script>
const money = p => '₹' + (p/100).toLocaleString('en-IN',{maximumFractionDigits:0});
const el = (h,c) => { const d=document.createElement('div'); d.className=c; d.innerHTML=h; return d; };
let DATA;

fetch('/api/run').then(r=>r.json()).then(run=>{
  DATA = run;
  hdr.textContent = `${run.orders.length} orders · ${run.settlements.length} bank lines · book date ${run.as_of}`;
  cTime.textContent = run.elapsed.toFixed(3)+'s';

  for (const o of run.orders)
    orders.appendChild(el(`${o.id} <span class=amt>${money(o.gross)}</span>`, 'row o-'+o.id));
  for (const s of run.settlements)
    setts.appendChild(el(`${s.id} <span class=amt>${money(s.type==='refund'?-s.net:s.net)}</span>`,
      'row s-'+s.id));

  play(run);
});

function pos(node){
  const g = grid.getBoundingClientRect(), r = node.getBoundingClientRect();
  return {x: r.left-g.left, y: r.top-g.top+r.height/2, w: r.width};
}
function line(a,b,colour){
  const p1=pos(a), p2=pos(b);
  const x1=p1.x+p1.w, x2=p2.x, mid=(x1+x2)/2;
  const path=document.createElementNS('http://www.w3.org/2000/svg','path');
  path.setAttribute('d',`M${x1},${p1.y} C${mid},${p1.y} ${mid},${p2.y} ${x2},${p2.y}`);
  path.setAttribute('stroke',colour); path.setAttribute('fill','none');
  path.setAttribute('stroke-width','1.2'); path.setAttribute('opacity','.85');
  svg.appendChild(path);
}

function play(run){
  svg.setAttribute('width', grid.scrollWidth); svg.setAttribute('height', grid.scrollHeight);
  let i=0, matched=0, flagged=0;
  const step = () => {
    if (i >= run.matches.length) return finish(run);
    const m = run.matches[i++];
    const srow = document.querySelector('.s-'+m.settlement);
    const colour = m.confident ? getComputedStyle(document.body).getPropertyValue('--ok')
                               : getComputedStyle(document.body).getPropertyValue('--warn');
    srow.classList.add('on', m.confident?'ok':'warn');
    for (const oid of m.orders){
      const orow = document.querySelector('.o-'+oid);
      orow.classList.add('on', m.confident?'ok':'warn');
      line(orow, srow, colour.trim());
    }
    if (m.confident) matched += m.orders.length; else flagged += m.orders.length;
    cMatched.textContent = matched; cFlagged.textContent = flagged;
    setTimeout(step, 45);
  };
  step();
}

function finish(run){
  // anything still faint was never matched -- that is the honest part of the picture
  for (const r of document.querySelectorAll('.row:not(.on)')) r.classList.add('open');
  let risk = 0;
  run.exceptions.forEach((e,i)=>setTimeout(()=>{
    risk += e.amount; cRisk.textContent = money(risk);
    cExc.textContent = i+1;
    const cls = e.amount===0 ? 'exc info' : (e.code.includes('LATE')?'exc soft':'exc');
    const node = el(`<div class=code>${e.code}</div>
      <div class=meta>${e.record} · ${money(e.amount)} · ${e.age_days}d</div>`, cls);
    node.onclick = ()=>show(e);
    excs.appendChild(node);
  }, i*35));
  const fee = run.exceptions.filter(e=>e.code==='FEE_VARIANCE'||e.code==='ZERO_MDR_VIOLATION')
                            .reduce((a,e)=>a+e.amount,0);
  cFee.textContent = money(fee);
  const a = run.accuracy;
  hdr.textContent += ` · ${a.true_positives} true matches, ${a.false_positives.length} false`
    + ` · ${a.false_matches_caught.length} lookalikes rejected`;
}

function show(e){
  dbody.innerHTML = `<h3>${e.code} — ${e.record}</h3>
    <p class=k>why</p><p>${e.explanation}</p>
    ${e.llm_explanation?`<p class=k>in plain english</p><p>${e.llm_explanation}</p>`:''}
    <p class=k>suggested action</p><p>${e.action}</p>
    <p class=k>money at risk</p><p>${money(e.amount)} · ${e.age_days} days old</p>`;
  detail.style.display='block';
}
</script>"""


@app.get("/batch")
def index_batch():
    return PAGE


if __name__ == "__main__":
    live.start()
    app.run(debug=False, port=5000, threaded=True)
