# -*- coding: utf-8 -*-
"""
Shared service layer for the agent (MCP) channel.

Design rule: this module never re-implements a scheme rule. Eligibility, field
validation, the one-active-application rule and the deadline all come from the
portal's own validate_application() / create_application() / active_application(),
so an assistant can never admit someone the counter or the web form would refuse.
"""
import os
import re
import sys
import json
import hashlib
import secrets
from datetime import datetime, timedelta

sys.path.insert(0, os.environ.get("PORTAL_PATH", "/app"))
import app as portal          # noqa: E402  the portal's own module (rules + db access)
import oauth as portal_oauth  # noqa: E402

DRAFT_FIELDS = ("applicant_name", "dob", "gender", "marital_status", "husband_name",
                "village", "block", "bank_account", "ifsc")
PREVIEW_MINUTES = 15
UPLOAD_LINK_MINUTES = 20
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
FIELD_HELP = {
    "applicant_name": "full name as on the age proof",
    "dob": "date of birth as DD/MM/YYYY",
    "gender": "Female, Male or Other",
    "marital_status": "Single, Married, Widowed, Divorced",
    "husband_name": "husband's name (optional)",
    "village": "village",
    "block": "one of: " + ", ".join(portal.BLOCKS),
    "bank_account": "bank account number (9 to 18 digits)",
    "ifsc": "bank IFSC code, 11 characters, e.g. SBIN0003077",
}


# ---------------------------------------------------------------- plumbing
def ensure_schema():
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_drafts (
            mobile          VARCHAR(20) PRIMARY KEY,
            data            TEXT NOT NULL DEFAULT '{}',
            doc_path        VARCHAR(300) NOT NULL DEFAULT '',
            preview_token   VARCHAR(64),
            preview_hash    VARCHAR(64),
            preview_expires TIMESTAMP,
            submitted_no    VARCHAR(20),
            updated_at      TIMESTAMP
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_upload_links (
            token      VARCHAR(64) PRIMARY KEY,
            mobile     VARCHAR(20) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used       BOOLEAN NOT NULL DEFAULT FALSE
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_signouts (
            subject VARCHAR(80) PRIMARY KEY,
            at      TIMESTAMP NOT NULL
        )""")
    conn.commit()
    cur.close(); conn.close()


def _db():
    conn = portal.get_db()
    return conn, conn.cursor()


def mobile_of(subject):
    """Token subject is 'citizen:<mobile>'. The mobile is only ever taken from here."""
    if not subject or not subject.startswith("citizen:"):
        raise PermissionError("this tool needs a signed-in citizen")
    return subject.split(":", 1)[1]


def mask_account(acct):
    return ("*" * max(len(acct) - 4, 0)) + acct[-4:] if acct else ""


def _clean_text(v, maxlen=100):
    # Applicant-written text is data. Strip control characters and cap the length.
    return re.sub(r"[\x00-\x1f\x7f]", " ", portal.sanitize(v, maxlen))


# ---------------------------------------------------------------- scheme info
def scheme_info():
    now = portal.now_ist()
    open_ = now <= portal.SCHEME_DEADLINE
    return {
        "scheme": "Old Age Pension, Government of Purvanchal, Department of Social Welfare",
        "amount": "Rs 250 per month",
        "who_can_apply": "Residents of Purvanchal aged %d or above." % portal.MIN_AGE,
        "applications_open": open_,
        "last_date_to_apply": portal.SCHEME_DEADLINE.strftime("%d %B %Y, %I:%M %p IST"),
        "blocks": portal.BLOCKS,
        "documents_needed": "One age proof (JPG, PNG or PDF, up to 5 MB).",
        "details_needed": {k: FIELD_HELP[k] for k in DRAFT_FIELDS},
        "decision_time": ("A block officer approves or rejects. If nobody decides within %d "
                          "days, the application is approved automatically." % portal.SLA_DAYS),
        "one_application_rule": "Only one live application per mobile number. A pending "
                                "application can be withdrawn to apply afresh.",
        "how_to_apply": "Save details, upload the age proof, preview, then confirm to submit.",
        "today_ist": now.strftime("%d/%m/%Y"),
    }


# ---------------------------------------------------------------- drafts
def _load(cur, mobile):
    cur.execute("SELECT data, doc_path, submitted_no FROM agent_drafts WHERE mobile=%s", (mobile,))
    row = cur.fetchone()
    if not row:
        return {}, "", None
    return json.loads(row[0] or "{}"), row[1] or "", row[2]


def _store(cur, mobile, data, doc_path, submitted_no=None):
    cur.execute("""INSERT INTO agent_drafts (mobile, data, doc_path, submitted_no, updated_at,
                                             preview_token, preview_hash, preview_expires)
                   VALUES (%s,%s,%s,%s,%s,NULL,NULL,NULL)
                   ON CONFLICT (mobile) DO UPDATE SET data=EXCLUDED.data, doc_path=EXCLUDED.doc_path,
                   submitted_no=EXCLUDED.submitted_no, updated_at=EXCLUDED.updated_at,
                   preview_token=NULL, preview_hash=NULL, preview_expires=NULL""",
                (mobile, json.dumps(data), doc_path, submitted_no, datetime.now()))


def _problems(data, doc_path):
    errors, cleaned = portal.validate_application(data, doc_path)
    return errors, cleaned


def _eligibility_note(data):
    dob = portal.parse_dob(data.get("dob"))
    if not dob:
        return None
    today = portal.now_ist().date()
    if portal.age_on(dob, today) >= portal.MIN_AGE:
        return None
    try:
        eligible_on = dob.replace(year=dob.year + portal.MIN_AGE)
    except ValueError:                       # 29 February
        eligible_on = dob.replace(year=dob.year + portal.MIN_AGE, day=28)
    return ("Not eligible yet: the applicant is below %d. They turn %d on %s. This is the "
            "scheme's rule; it cannot be changed from here." %
            (portal.MIN_AGE, portal.MIN_AGE, eligible_on.strftime("%d/%m/%Y")))


def save_draft(mobile, fields):
    conn, cur = _db()
    data, doc_path, submitted = _load(cur, mobile)
    if submitted:                            # a fresh application starts a fresh draft
        data, doc_path = {}, ""
    unknown = [k for k in fields if k not in DRAFT_FIELDS]
    for k in DRAFT_FIELDS:
        if k in fields and fields[k] is not None:
            v = str(fields[k])
            data[k] = _clean_text(v, 100)
    _store(cur, mobile, data, doc_path)
    conn.commit()
    cur.close(); conn.close()
    errors, _ = _problems(data, doc_path)
    return {
        "saved": {k: (mask_account(v) if k == "bank_account" else v) for k, v in data.items()},
        "age_proof_uploaded": bool(doc_path),
        "still_to_fix_or_provide": errors,
        "eligibility": _eligibility_note(data),
        "ignored_unknown_fields": unknown,
    }


# ---------------------------------------------------------------- age proof
def _safe_name(filename):
    base = os.path.basename(filename or "").lower()
    base = re.sub(r"[^a-z0-9._-]", "_", base)
    return base[-80:]


def store_doc(mobile, filename, content):
    name = _safe_name(filename)
    if name.rsplit(".", 1)[-1] not in ("pdf", "jpg", "jpeg", "png"):
        return {"ok": False, "error": "Please upload a JPG, PNG or PDF file."}
    if not content:
        return {"ok": False, "error": "The file is empty."}
    if len(content) > MAX_UPLOAD_BYTES:
        return {"ok": False, "error": "File is larger than 5 MB. Please upload a smaller file."}
    os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(portal.UPLOAD_DIR, "%s_%s" % (mobile, name))
    with open(path, "wb") as out:
        out.write(content)
    conn, cur = _db()
    data, _, submitted = _load(cur, mobile)
    if submitted:
        data = {}
    _store(cur, mobile, data, path)
    conn.commit()
    cur.close(); conn.close()
    return {"ok": True, "message": "Age proof received."}


def make_upload_link(mobile, base_url):
    token = secrets.token_urlsafe(24)
    conn, cur = _db()
    cur.execute("INSERT INTO agent_upload_links (token, mobile, expires_at) VALUES (%s,%s,%s)",
                (token, mobile, datetime.now() + timedelta(minutes=UPLOAD_LINK_MINUTES)))
    conn.commit()
    cur.close(); conn.close()
    return {"upload_link": base_url.rstrip("/") + "/upload/" + token,
            "valid_minutes": UPLOAD_LINK_MINUTES,
            "tell_the_citizen": "Open this link on the phone that has the age proof photo, "
                                "choose the photo, and press Upload. Then come back here."}


def link_mobile(token):
    conn, cur = _db()
    cur.execute("SELECT mobile FROM agent_upload_links WHERE token=%s AND NOT used AND expires_at > %s",
                (token, datetime.now()))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None


def consume_link(token):
    conn, cur = _db()
    cur.execute("UPDATE agent_upload_links SET used=TRUE WHERE token=%s", (token,))
    conn.commit()
    cur.close(); conn.close()


# ---------------------------------------------------------------- preview / submit
def _hash(cleaned):
    keys = ("applicant_name", "dob", "gender", "marital_status", "husband_name", "village",
            "block", "bank_account", "ifsc", "doc_path")
    blob = json.dumps({k: str(cleaned.get(k)) for k in keys}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def preview(mobile):
    conn, cur = _db()
    data, doc_path, submitted = _load(cur, mobile)
    if submitted and not data:
        cur.close(); conn.close()
        return {"ready": False, "problems": ["There is no draft. Save details first."]}
    existing = portal.active_application(cur, mobile)
    errors, cleaned = _problems(data, doc_path)
    if existing:
        errors.append("An application already exists for this mobile number: %s (%s). A "
                      "pending application can be withdrawn first if a new one is needed."
                      % (existing[1], existing[2]))
    note = _eligibility_note(data)
    if errors:
        cur.close(); conn.close()
        return {"ready": False, "problems": errors, "eligibility": note}
    token = secrets.token_urlsafe(16)
    cur.execute("UPDATE agent_drafts SET preview_token=%s, preview_hash=%s, preview_expires=%s "
                "WHERE mobile=%s",
                (token, _hash(cleaned), datetime.now() + timedelta(minutes=PREVIEW_MINUTES), mobile))
    conn.commit()
    cur.close(); conn.close()
    return {
        "ready": True,
        "read_this_back_to_the_citizen": {
            "name": cleaned["applicant_name"], "date_of_birth": cleaned["dob"].strftime("%d/%m/%Y"),
            "gender": cleaned["gender"], "marital_status": cleaned["marital_status"],
            "husband_name": cleaned["husband_name"], "village": cleaned["village"],
            "block": cleaned["block"], "bank_account": mask_account(cleaned["bank_account"]),
            "ifsc": cleaned["ifsc"], "age_proof": "uploaded",
        },
        "preview_token": token,
        "valid_minutes": PREVIEW_MINUTES,
        "next_step": "Read these details back to the citizen. Only if they clearly say yes, "
                     "call submit_application with this preview_token and confirmed=true.",
    }


def submit(mobile, preview_token, confirmed):
    if confirmed is not True:
        return {"submitted": False, "error": "The citizen has not confirmed. Do not submit "
                                              "until they clearly agree."}
    conn, cur = _db()
    cur.execute("SELECT data, doc_path, preview_token, preview_hash, preview_expires, submitted_no "
                "FROM agent_drafts WHERE mobile=%s FOR UPDATE", (mobile,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {"submitted": False, "error": "No draft found. Save details and preview first."}
    data, doc_path, tok, phash, expires, submitted_no = json.loads(row[0] or "{}"), row[1], row[2], row[3], row[4], row[5]
    # Safe retry: repeating the same confirmed call returns the same application number.
    if submitted_no and tok and secrets.compare_digest(tok, preview_token or ""):
        cur.close(); conn.close()
        return {"submitted": True, "application_no": submitted_no, "already_submitted": True}
    if not tok or not secrets.compare_digest(tok, preview_token or ""):
        cur.close(); conn.close()
        return {"submitted": False, "error": "Unknown or outdated preview_token. Call "
                                              "preview_application again."}
    if expires and expires < datetime.now():
        cur.close(); conn.close()
        return {"submitted": False, "error": "The preview expired. Call preview_application again."}
    errors, cleaned = _problems(data, doc_path)
    if _hash(cleaned) != phash:
        cur.close(); conn.close()
        return {"submitted": False, "error": "Details changed after the preview. Preview again."}
    existing = portal.active_application(cur, mobile)
    if errors or existing:
        cur.close(); conn.close()
        return {"submitted": False, "problems": errors or ["An application already exists."]}
    new_id, app_no = portal.create_application(cur, mobile, cleaned)
    cur.execute("UPDATE agent_drafts SET submitted_no=%s, data='{}', doc_path='', "
                "preview_hash=NULL, preview_expires=NULL WHERE mobile=%s",
                (app_no, mobile))
    cur.execute("UPDATE audit_log SET actor = actor || ' via-assistant' "
                "WHERE application_id=%s AND action='SUBMIT'", (new_id,))
    conn.commit()
    cur.close(); conn.close()
    portal.send_sms(mobile, "Sewa Setu: application %s received. An AI assistant helped you "
                            "apply. A block officer will decide within %d days."
                    % (app_no, portal.SLA_DAYS))
    return {"submitted": True, "application_no": app_no,
            "what_happens_next": "A block officer will decide within %d days; if nobody does, "
                                 "it is approved automatically. Ask for the status any time."
                                 % portal.SLA_DAYS}


# ---------------------------------------------------------------- status / withdraw
def status(mobile):
    conn, cur = _db()
    cur.execute("SELECT id, application_no, status, submitted_at, decided_at, block FROM applications "
                "WHERE mobile=%s ORDER BY submitted_at DESC LIMIT 5", (mobile,))
    rows = cur.fetchall()
    out = []
    for app_id, no, st, sub, dec, block in rows:
        item = {"application_no": no, "status": st, "block": block,
                "submitted_on": sub.strftime("%d/%m/%Y") if sub else None}
        if st == "PENDING" and sub:
            waited = (portal.now_ist() - sub).days
            item["days_waiting"] = waited
            item["auto_approval_date"] = (sub + timedelta(days=portal.SLA_DAYS)).strftime("%d/%m/%Y")
        if dec:
            item["decided_on"] = dec.strftime("%d/%m/%Y")
        if st in ("APPROVED", "REJECTED"):
            cur.execute("SELECT note FROM audit_log WHERE application_id=%s AND action=%s "
                        "ORDER BY at DESC LIMIT 1", (app_id, st))
            r = cur.fetchone()
            if r and r[0]:
                item["officer_reason"] = r[0]
        out.append(item)
    cur.close(); conn.close()
    return {"applications": out} if out else {"applications": [],
                                              "message": "No application found for this mobile number."}


def withdraw(mobile, application_no, confirmed):
    if confirmed is not True:
        return {"withdrawn": False, "error": "Ask the citizen to confirm first; withdrawing "
                                              "means starting again to apply."}
    conn, cur = _db()
    cur.execute("UPDATE applications SET status='WITHDRAWN', decided_at=%s, decided_by='APPLICANT' "
                "WHERE mobile=%s AND application_no=%s AND status='PENDING' RETURNING id",
                (datetime.now(), mobile, application_no))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return {"withdrawn": False, "error": "Only your own pending application can be withdrawn."}
    portal.write_audit(cur, row[0], "WITHDRAW", "applicant:%s via-assistant" % mobile)
    conn.commit()
    cur.close(); conn.close()
    return {"withdrawn": True, "application_no": application_no,
            "message": "Withdrawn. A new application can now be made."}


# ---------------------------------------------------------------- sign-out
def sign_out(subject):
    mobile = mobile_of(subject)
    revoked = portal_oauth._revoke_grants(subject)
    conn, cur = _db()
    cur.execute("DELETE FROM agent_drafts WHERE mobile=%s", (mobile,))
    cur.execute("DELETE FROM agent_upload_links WHERE mobile=%s", (mobile,))
    cur.execute("INSERT INTO agent_signouts (subject, at) VALUES (%s,%s) "
                "ON CONFLICT (subject) DO UPDATE SET at=EXCLUDED.at", (subject, datetime.now()))
    conn.commit()
    cur.close(); conn.close()
    return {"signed_out": True, "sessions_ended": revoked,
            "message": "Signed out. Draft and any unused upload link were deleted."}


def signed_out_after(subject):
    conn, cur = _db()
    cur.execute("SELECT at FROM agent_signouts WHERE subject=%s", (subject,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None
