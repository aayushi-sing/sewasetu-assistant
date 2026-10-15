# -*- coding: utf-8 -*-
"""End-to-end check of the citizen MCP against a running portal + citizen server.
Run:  PORTAL_PATH=../app python tests/e2e_citizen.py   (from the agent/ folder)
It mints tokens exactly as the portal's OAuth server does, connects as a real MCP client,
plays one persona, and then checks what is actually in the database."""
import os, sys, json, base64, asyncio, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.environ.get("PORTAL_PATH", "/app"))
import app as portal, oauth as po
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("CITIZEN_RESOURCE_URL", "http://localhost:9001/mcp")
MOBILE = "9000000001"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 200).decode()
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("  -> " + str(detail) if not ok else ""))


def token(sub, aud=URL, scope="citizen", role="citizen"):
    return po.mint_access_token(sub, role, scope, aud, "test-client")[0]


async def call(session, name, **args):
    r = await session.call_tool(name, args)
    return json.loads(r.content[0].text) if r.content else {}


async def main():
    conn = portal.get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM applications WHERE mobile=%s", (MOBILE,))
    cur.execute("DELETE FROM agent_drafts WHERE mobile=%s", (MOBILE,))
    cur.execute("DELETE FROM agent_signouts WHERE subject=%s", ("citizen:" + MOBILE,))
    conn.commit()
    sub = "citizen:" + MOBILE

    # 1. bad tokens are refused at the door
    import httpx
    for label, tok in (("wrong audience", token(sub, aud="http://other/mcp")),
                       ("officer token", token("officer:admin", scope="officer", role="officer")),
                       ("garbage", "abc.def.ghi")):
        r = httpx.post(URL, headers={"Authorization": "Bearer " + tok, "content-type": "application/json",
                                     "accept": "application/json, text/event-stream"}, json={})
        check("rejects " + label, r.status_code == 401, r.status_code)

    hdr = {"Authorization": "Bearer " + token(sub)}
    async with streamablehttp_client(URL, headers=hdr) as (rd, wr, _):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            check("citizen sees only citizen tools",
                  "get_application_status" in tools and not any("approve" in t or "list_pending" in t for t in tools), tools)
            info = await call(s, "get_scheme_info")
            check("scheme info is current", "250" in info["amount"] and info["applications_open"] is True, info)

            # Persona: Kamala Devi, 70, Sonari, grandson types
            d = await call(s, "save_draft", applicant_name="Kamala Devi", dob="12/03/1956",
                           gender="Female", marital_status="Widowed", village="Pathar Gaon",
                           block="Sonari", bank_account="1234 5678 9012", ifsc="sbin0003077")
            check("draft lists only the age proof as missing",
                  d["still_to_fix_or_provide"] == ["age proof document is required"], d)
            p = await call(s, "preview_application")
            check("preview refused without age proof", p["ready"] is False, p)

            bad = await call(s, "upload_age_proof", filename="proof.exe", content_base64=PNG)
            check("rejects non-image upload", bad["ok"] is False, bad)
            ok = await call(s, "upload_age_proof", filename="../../etc/Proof.PNG", content_base64=PNG)
            check("accepts png (path stripped)", ok["ok"] is True, ok)
            check("no file escaped upload dir",
                  all(os.path.dirname(os.path.join(portal.UPLOAD_DIR, f)) == portal.UPLOAD_DIR
                      for f in os.listdir(portal.UPLOAD_DIR)))

            p = await call(s, "preview_application")
            check("preview ready, account masked",
                  p["ready"] and p["read_this_back_to_the_citizen"]["bank_account"].endswith("9012")
                  and "1234" not in p["read_this_back_to_the_citizen"]["bank_account"], p)
            tok = p.get("preview_token", "")

            r = await call(s, "submit_application", preview_token=tok, confirmed=False)
            check("submit refused without confirmation", r["submitted"] is False, r)
            r = await call(s, "submit_application", preview_token="wrong", confirmed=True)
            check("submit refused with wrong token", r["submitted"] is False, r)
            r = await call(s, "submit_application", preview_token=tok, confirmed=True)
            check("submit succeeds", r.get("submitted") is True, r)
            app_no = r.get("application_no")
            r2 = await call(s, "submit_application", preview_token=tok, confirmed=True)
            check("retry returns the same application number", r2.get("application_no") == app_no, r2)

            cur.execute("SELECT count(*), min(status) FROM applications WHERE mobile=%s", (MOBILE,))
            check("exactly one PENDING row in the portal DB", cur.fetchone() == (1, "PENDING"))
            cur.execute("SELECT actor FROM audit_log a JOIN applications p ON p.id=a.application_id "
                        "WHERE p.mobile=%s AND a.action='SUBMIT'", (MOBILE,))
            check("audit trail marks the assistant channel", "via-assistant" in cur.fetchone()[0])

            st = await call(s, "get_application_status")
            check("status visible", st["applications"][0]["application_no"] == app_no
                  and st["applications"][0]["status"] == "PENDING", st)

            # second application must be refused
            await call(s, "save_draft", applicant_name="Kamala Devi", dob="12/03/1956", gender="Female",
                       marital_status="Widowed", village="Pathar Gaon", block="Sonari",
                       bank_account="123456789012", ifsc="SBIN0003077")
            await call(s, "upload_age_proof", filename="p.png", content_base64=PNG)
            p = await call(s, "preview_application")
            check("second live application refused", p["ready"] is False, p)

            w = await call(s, "withdraw_application", application_no=app_no, confirmed=True)
            check("withdraw works", w.get("withdrawn") is True, w)
            so = await call(s, "sign_out")
            check("sign_out ok", so.get("signed_out") is True, so)

    # 2. under-60: Raghunath, 59
    sub2 = "citizen:9000000002"
    cur.execute("DELETE FROM agent_drafts WHERE mobile='9000000002'"); conn.commit()
    async with streamablehttp_client(URL, headers={"Authorization": "Bearer " + token(sub2)}) as (rd, wr, _):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            d = await call(s, "save_draft", applicant_name="Raghunath Borah", dob="20/11/1967",
                           gender="Male", marital_status="Married", village="Namti Gaon",
                           block="Namti", bank_account="123456789012", ifsc="SBIN0003077")
            check("59-year-old told not eligible with a date",
                  d["eligibility"] and "Not eligible yet" in d["eligibility"], d)
            await call(s, "upload_age_proof", filename="p.png", content_base64=PNG)
            p = await call(s, "preview_application")
            check("59-year-old cannot reach submit", p["ready"] is False, p)

    # 3. a signed-out token is dead even before it expires
    r = httpx.post(URL, headers={"Authorization": "Bearer " + hdr["Authorization"].split()[1],
                                 "content-type": "application/json",
                                 "accept": "application/json, text/event-stream"}, json={})
    check("signed-out token rejected", r.status_code == 401, r.status_code)

    cur.execute("DELETE FROM applications WHERE mobile IN ('9000000001','9000000002')")
    conn.commit()
    print("\n%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)

asyncio.run(main())
