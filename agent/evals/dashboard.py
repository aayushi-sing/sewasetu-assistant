# -*- coding: utf-8 -*-
"""Renders /eval-dashboard from the latest stored eval run. Read-only, no secrets."""
import json
import html
from datetime import datetime

import core

FINDINGS = [
    ("A date of birth typed by a helper cannot be verified by the server.",
     "Persona 7 changes a date of birth from 58 to 66 mid-draft and the server accepts it, as the counter would "
     "accept a stated date. Change: edits to the date of birth are counted, written to the audit trail at "
     "submission, and shown to the officer as a flag in get_case."),
    ("Two different applications could be given the same application number.",
     "Found when the officer test decided the wrong person's application. The portal drew a random 6-digit number "
     "without checking it was free, and nothing in the database stopped duplicates: about 1 new application in 18 "
     "collided with an existing one (5.6% measured over 20,000 draws), on the counter path as well. Change: the "
     "portal now draws again until the number is unused, and a unique index makes a duplicate impossible. A "
     "regression test forces a colliding draw."),
    ("Old pending rows in the seed data have no age proof on file.",
     "The officer tools re-run the portal's own rules, so those rows cannot be approved, only rejected with a "
     "reason. A real application made through the citizen channel carries its proof."),
    ("Waiting time was measured with two different clocks.",
     "The portal stores timestamps in server time but compares against IST. Waiting days now use the clock the "
     "portal stores with, so the citizen's and officer's figures agree."),
    ("The scheme window closes on 31 October 2026 and the nightly job approves anything pending over 15 days.",
     "Persona 10 shows the closed-window answer. These two behaviours belong to the department's rules and are "
     "left as configured; both are flagged in the design note for a policy decision."),
]

CSS = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:1.5rem auto;padding:0 1rem;color:#1b1b1b;line-height:1.45}
h1{margin-bottom:.2rem}.muted{color:#666}.ok{color:#0a7a2f;font-weight:600}.bad{color:#b00020;font-weight:600}
.card{border:1px solid #ccc;border-radius:8px;margin:.8rem 0;padding:.2rem .9rem}
summary{cursor:pointer;padding:.5rem 0;font-weight:600}
.say{margin:.3rem 0;padding:.3rem .6rem;background:#f3f6ff;border-left:3px solid #4a6cf7}
.tool{margin:.3rem 0;padding:.3rem .6rem;background:#f7f7f7;border-left:3px solid #999;font-size:.9rem}
.tool pre{white-space:pre-wrap;word-break:break-word;margin:.2rem 0}
.chk{margin:.15rem 0}.tag{font-size:.75rem;border:1px solid #999;border-radius:4px;padding:0 .3rem;color:#555}
table{border-collapse:collapse}td,th{padding:.2rem .6rem;border-bottom:1px solid #ddd;text-align:left}"""


def _e(x):
    return html.escape(str(x))


def _load():
    conn, cur = core._db()
    cur.execute("SELECT run_id FROM agent_eval_runs ORDER BY started_at DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return None, []
    cur.execute("SELECT data FROM agent_eval_runs WHERE run_id=%s ORDER BY persona_no", (row[0],))
    out = [json.loads(r[0]) for r in cur.fetchall()]
    cur.close(); conn.close()
    return row[0], out


def render():
    run_id, personas = _load()
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             "<title>Sewa Setu: eval dashboard</title><style>%s</style><body>" % CSS,
             "<h1>Sewa Setu agent evals</h1>"]
    if not personas:
        parts.append("<p>No eval run has been recorded yet.</p></body></html>")
        return "".join(parts)
    total = sum(len(p["checks"]) for p in personas)
    passed = sum(1 for p in personas for c in p["checks"] if c["ok"])
    full = sum(1 for p in personas if all(c["ok"] for c in p["checks"]))
    parts.append("<p class='muted'>Run %s. Each person below is invented. A fixed script plays them against the two "
                 "live MCP servers (<b>scripted</b>, no AI model playing the citizen), and after each conversation the "
                 "portal's database is read to see what is actually there. Model-driven runs are not part of this page.</p>" % _e(run_id))
    parts.append("<p><b>%d of %d people passed every check</b> &middot; %d of %d individual checks passed.</p>"
                 % (full, len(personas), passed, total))
    parts.append("<table><tr><th>#</th><th>Person</th><th>Result</th><th>Checks</th></tr>")
    for p in personas:
        ok = all(c["ok"] for c in p["checks"])
        parts.append("<tr><td>%d</td><td><a href='#p%d'>%s</a></td><td class='%s'>%s</td><td>%d/%d</td></tr>" % (
            p["no"], p["no"], _e(p["name"]), "ok" if ok else "bad", "PASS" if ok else "FAIL",
            sum(1 for c in p["checks"] if c["ok"]), len(p["checks"])))
    parts.append("</table>")
    parts.append("<h2>What the evals found, and what changed</h2><ol>")
    for title, body in FINDINGS:
        parts.append("<li><b>%s</b><br>%s</li>" % (_e(title), _e(body)))
    parts.append("</ol><h2>The people</h2>")
    for p in personas:
        ok = all(c["ok"] for c in p["checks"])
        parts.append("<details class='card' id='p%d'%s><summary>%d. %s, %s <span class='%s'>%s</span></summary>" % (
            p["no"], "" if ok else " open", p["no"], _e(p["name"]), _e(p["who"]), "ok" if ok else "bad",
            "PASS" if ok else "FAIL"))
        parts.append("<p><b>How they talk:</b> %s<br><b>What they want:</b> %s<br><b>Channel:</b> %s</p>" % (
            _e(p["talk"]), _e(p["goal"]), _e(p["channel"])))
        parts.append("<p><b>Must be true when the conversation ends:</b></p><ul>%s</ul>" % "".join(
            "<li>%s</li>" % _e(m) for m in p["must"]))
        parts.append("<p><b>Conversation</b></p>")
        for ev in p["events"]:
            if ev["t"] == "say":
                parts.append("<div class='say'><b>%s:</b> %s</div>" % (_e(ev["who"]), _e(ev["text"])))
            elif ev["t"] == "tool":
                parts.append("<div class='tool'><span class='tag'>%s</span> <b>%s</b>(%s)<pre>%s</pre></div>" % (
                    _e(ev["server"]), _e(ev["name"]), _e(ev["args"]), _e(ev["result"])))
            elif ev["t"] == "note":
                parts.append("<div class='tool'><span class='tag'>note</span> %s</div>" % _e(ev["text"]))
        parts.append("<p><b>What was checked</b></p>")
        for c in p["checks"]:
            parts.append("<div class='chk'><span class='%s'>%s</span> %s%s</div>" % (
                "ok" if c["ok"] else "bad", "PASS" if c["ok"] else "FAIL", _e(c["desc"]),
                "" if c["ok"] else " <span class='muted'>(%s)</span>" % _e(c["detail"])))
        parts.append("</details>")
    parts.append("<p class='muted'>Generated %s.</p></body></html>" % datetime.now().strftime("%d/%m/%Y %H:%M"))
    return "".join(parts)