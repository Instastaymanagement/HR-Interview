#!/usr/bin/env python3
"""Every 2 minutes: screen job candidates over Quo text, and close out a vacancy once someone is hired.

The candidates live on the monday.com "Vacancies" board: each group is one
vacancy (e.g. "Part-Time Cleaner – London ON") and each item is a candidate,
with a Phone, a Hiring Stage and "Flags / Ask on Call" notes.

Each run, for every vacancy that has nobody marked Hired yet:
1. Candidates in a contact stage (default "Shortlist – Call") get a first text
   from the Quo line for the vacancy's country, and move to "Contacted".
2. When a contacted candidate replies, Claude reads the whole thread and texts
   back the next question, until it knows enough about what the role needs
   (experience, car/licence, availability, location, the candidate's flags).
   It then sends a short sign-off and sets the stage to "Good Fit" or
   "Not a Fit", with its reasoning in "Screening Notes" and as an update.
3. Once anyone in the vacancy is set to "Hired", the automation stops screening
   that vacancy, thanks every other candidate it has texted, tells them the role
   is filled and that we'll be in touch about future openings, and sets them to
   "Not Selected".

Candidates that a person has moved to "Interview Booked" or "Trial Clean" are
left to that person. Texts are only sent between SEND_FROM_HOUR and
SEND_UNTIL_HOUR local time; anything due outside those hours waits for the
next run inside them.

Environment:
  QUO_API_KEY           Quo (OpenPhone) API key (required)
  MONDAY_API_TOKEN      monday.com API token (required)
  ANTHROPIC_API_KEY     Claude API key (required)
  ANTHROPIC_WORKSPACE_ID  workspace for the key; needed only if the key isn't scoped to one
  QUO_NUMBERS           Quo number per country, e.g. "+12267813731=Canada,+18573538919=USA"
  VACANCIES_BOARD       monday board id of the Vacancies board (default 18435820027)
  CONTACT_STAGES        comma-separated Hiring Stages to start texting (default "Shortlist – Call")
  DRY_RUN               "true" to log what would be sent without texting anyone or changing monday
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import anthropic

MONDAY_API = "https://api.monday.com/v2"
QUO_API = "https://api.openphone.com/v1"
MODEL = "claude-opus-5-5"

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
VACANCIES_BOARD = int(os.environ.get("VACANCIES_BOARD") or 18435820027)
CONTACT_STAGES = [s.strip() for s in (os.environ.get("CONTACT_STAGES") or "Shortlist – Call").split(",") if s.strip()]
ROLES_FILE = os.environ.get("ROLES_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles.json")

SEND_FROM_HOUR = int(os.environ.get("SEND_FROM_HOUR") or 9)    # local time, inclusive
SEND_UNTIL_HOUR = int(os.environ.get("SEND_UNTIL_HOUR") or 20)  # local time, exclusive
MAX_OUR_MESSAGES = 8      # texts we send one candidate before we must decide
TIMEZONES = {"Canada": "America/Toronto", "USA": "America/New_York"}

# Hiring Stage labels
CONTACTED, SCREENING = "Contacted", "Screening"
GOOD_FIT, NOT_A_FIT = "Good Fit", "Not a Fit"
HIRED, NOT_SELECTED, WITHDRAWN = "Hired", "Not Selected", "Withdrawn"
HANDED_OVER = {"Interview Booked", "Trial Clean"}   # a person has taken over

SYSTEM_PROMPT = """You text job candidates for Instastay, a short-term rental (Airbnb) management company, on behalf of its hiring team. You are screening them for one role over SMS.

You are given the role, what the team needs to find out, what the candidate's CV says, the specific points the team flagged for this candidate, and the text thread so far.

Your job: find out, one or two questions at a time, whether the candidate meets what the role needs, then stop.

How to write:
- Plain, warm, professional SMS. Usually 1-3 short sentences, under 320 characters. No emojis, no lists, no markdown.
- Use the candidate's first name in the first message only.
- Ask about what the team still needs to know; don't re-ask what the candidate already answered or what the CV clearly settles.
- Cover the role's must-know points and the candidate's flagged points. Ask for specifics when an answer is vague ("Do you have your own car you could drive to properties?" rather than accepting "I can get there").
- The whole interview happens over text. Never suggest or offer a phone call, video call or in-person meeting, and don't ask when they're free to talk.
- Answer the candidate's questions only from the role information given. For anything else (exact pay, contract details, start date, anything not listed) say someone from the team will follow up with those details by text; never invent details or make promises.
- Never tell the candidate they are or aren't a fit, never offer the job, never mention scores or other candidates.
- If the candidate sincerely asks whether they are talking to a person or an automated system, be honest: say the messages are sent with an automated assistant and a team member reviews every conversation.

When to stop (decision):
- "continue": you still need answers. reply is your next text.
- "good_fit": you have enough and they meet the must-haves. reply is a short sign-off, e.g. thanking them and saying someone from the team will text them soon about next steps.
- "not_a_fit": you have enough and a must-have is clearly missing (e.g. no way to get to properties, not available at the times the role needs, not in the area). reply is the same kind of neutral, friendly sign-off; do not say why or that they weren't selected.
- "withdrawn": the candidate says they are no longer interested, asks you to stop, or found other work. reply is a brief polite acknowledgement (or empty if they asked not to be contacted).
- If you are told this is your last allowed message, you must choose good_fit, not_a_fit or withdrawn.

notes: 2-5 sentences for the hiring team: what the candidate said on each must-know point and flagged point, and why you decided as you did. Write "unknown" for points not covered.
car: "yes" if they confirmed their own car and licence, "no" if they said they don't have one, otherwise "unknown"."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "decision": {"type": "string", "enum": ["continue", "good_fit", "not_a_fit", "withdrawn"]},
        "notes": {"type": "string"},
        "car": {"type": "string", "enum": ["yes", "no", "unknown"]},
    },
    "required": ["reply", "decision", "notes", "car"],
    "additionalProperties": False,
}

CLOSING_MESSAGE = ("Hi {first}, thank you for your interest in the {role} role with Instastay and for taking the time "
                   "to chat with us. We've now filled the position, but we enjoyed hearing from you and will keep your "
                   "details on file. We'll be in touch if another opening comes up that's a good fit for you. All the best!")


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- HTTP

class HTTPStatusError(RuntimeError):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status


def http_json(url, *, headers, body=None, method=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                log(f"  HTTP {e.code} from {url}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise HTTPStatusError(e.code, url, e.read().decode()) from e
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def monday(query, variables=None):
    resp = http_json(MONDAY_API, headers={
        "Authorization": os.environ["MONDAY_API_TOKEN"],
        "Content-Type": "application/json",
        "API-Version": "2026-07",
    }, body={"query": query, "variables": variables or {}})
    if resp.get("errors"):
        raise RuntimeError(f"monday API error: {json.dumps(resp['errors'])[:1000]}")
    return resp["data"]


def quo(path, params=None, body=None):
    url = f"{QUO_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Authorization": os.environ["QUO_API_KEY"], "Accept": "application/json",
               # Quo's firewall rejects Python's default User-Agent, so name the client.
               "User-Agent": "Instastay-HR-Interview/1.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return http_json(url, headers=headers, body=body)


def quo_pages(path, params, limit=None):
    token, count = None, 0
    while True:
        resp = quo(path, {**params, **({"pageToken": token} if token else {})})
        for row in resp.get("data") or []:
            yield row
            count += 1
            if limit and count >= limit:
                return
        token = resp.get("nextPageToken")
        if not token:
            return


# -------------------------------------------------------------------------- helpers

def iso_ts(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def digits_phone(value):
    """'+1 (226) 700-9127' -> '+12267009127'; returns '' if it doesn't look like a phone number."""
    raw = re.sub(r"[^\d+]", "", value or "")
    if not raw:
        return ""
    if not raw.startswith("+"):
        raw = "+1" + raw if len(raw) == 10 else "+" + raw
    return raw if len(raw) >= 11 else ""


def first_name(name):
    return (name or "").strip().split(" ")[0] or "there"


def parse_quo_numbers(spec):
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            num, country = part.split("=", 1)
            out[country.strip()] = digits_phone(num)
    return out


def vacancy_country(group_title):
    """Country of a vacancy from its group title, e.g. 'Part-Time Cleaner – London ON' -> Canada."""
    if re.search(r"\b(ON|Ontario|BC|AB|QC|NS|MB|SK|Canada)\b", group_title or ""):
        return "Canada"
    return "USA"


def role_name(group_title):
    """'Part-Time Cleaner – London ON (posted Oct 14, 2026)' -> 'Part-Time Cleaner'."""
    return re.split(r"\s+[–-]\s+|\s*\(", group_title or "")[0].strip() or "open"


def parse_tracking(value):
    """Tracking column 'in=<ISO>;sent=<n>' -> (last handled inbound timestamp, texts we've sent)."""
    fields = dict(p.split("=", 1) for p in (value or "").split(";") if "=" in p)
    last_in = fields.get("in") or ""
    try:
        sent = int(fields.get("sent") or 0)
    except ValueError:
        sent = 0
    return (iso_ts(last_in) if last_in else 0.0), sent


def tracking_value(last_in_ts, sent):
    stamp = dt.datetime.fromtimestamp(last_in_ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last_in_ts else ""
    return f"in={stamp};sent={sent}"


def within_send_hours(country, now):
    local = dt.datetime.fromtimestamp(now, ZoneInfo(TIMEZONES.get(country, "America/Toronto")))
    return SEND_FROM_HOUR <= local.hour < SEND_UNTIL_HOUR


def load_roles(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def role_info(roles, group_title):
    """The roles.json entry whose key appears in the vacancy's group title (longest match wins)."""
    matches = [k for k in roles if k.lower() in (group_title or "").lower()]
    return roles[max(matches, key=len)] if matches else {}


# --------------------------------------------------------------------------- monday

def board_columns(board_id):
    data = monday("query($id:[ID!]){boards(ids:$id){columns{id title type}}}", {"id": [board_id]})
    return {c["title"]: c["id"] for c in data["boards"][0]["columns"]}


def load_candidates(board_id):
    cols = board_columns(board_id)
    for title in ("Phone", "Hiring Stage", "Quo Last Message", "Screening Notes"):
        if title not in cols:
            raise RuntimeError(f"Vacancies board has no '{title}' column (the API returned: {', '.join(cols)})")
    wanted = [cols[t] for t in ("Phone", "Hiring Stage", "Quo Last Message", "Screening Notes", "Location",
                                "Relevant Experience", "Flags / Ask on Call", "Car / Licence") if t in cols]
    query = """query($id:[ID!], $cols:[String!]){boards(ids:$id){items_page(limit:500){cursor
        items{id name group{id title} column_values(ids:$cols){id text}}}}}"""
    next_query = """query($cursor:String!, $cols:[String!]){next_items_page(limit:500, cursor:$cursor){cursor
        items{id name group{id title} column_values(ids:$cols){id text}}}}"""
    page = monday(query, {"id": [board_id], "cols": wanted})["boards"][0]["items_page"]
    items = list(page["items"])
    while page.get("cursor"):
        page = monday(next_query, {"cursor": page["cursor"], "cols": wanted})["next_items_page"]
        items.extend(page["items"])
    by_id = {v: k for k, v in cols.items()}
    out = []
    for it in items:
        vals = {by_id[c["id"]]: (c["text"] or "").strip() for c in it["column_values"] if c["id"] in by_id}
        last_in, sent = parse_tracking(vals.get("Quo Last Message"))
        out.append({
            "id": int(it["id"]), "name": it["name"], "group": it["group"]["title"],
            "phone": digits_phone(vals.get("Phone")), "stage": vals.get("Hiring Stage", ""),
            "tracking": vals.get("Quo Last Message", ""), "last_in": last_in, "sent": sent,
            "notes": vals.get("Screening Notes", ""), "location": vals.get("Location", ""),
            "experience": vals.get("Relevant Experience", ""), "flags": vals.get("Flags / Ask on Call", ""),
            "car": vals.get("Car / Licence", ""),
        })
    return cols, out


def update_candidate(cols, item_id, stage=None, tracking=None, notes=None, car=None):
    vals = {}
    if stage:
        vals[cols["Hiring Stage"]] = {"label": stage}
    if tracking is not None:
        vals[cols["Quo Last Message"]] = tracking
    if notes is not None:
        vals[cols["Screening Notes"]] = {"text": notes}
    if car and "Car / Licence" in cols:
        vals[cols["Car / Licence"]] = {"label": car}
    if not vals:
        return
    monday("""mutation($board:ID!, $item:ID!, $vals:JSON!){
        change_multiple_column_values(board_id:$board, item_id:$item, column_values:$vals,
                                      create_labels_if_missing:true){id}}""",
           {"board": VACANCIES_BOARD, "item": item_id, "vals": json.dumps(vals)})


def post_update(item_id, body):
    monday("mutation($item:ID!, $body:String!){create_update(item_id:$item, body:$body){id}}",
           {"item": item_id, "body": body})


# ---------------------------------------------------------------------------- Quo

def quo_lines(numbers):
    """Country -> Quo phone number id, for the numbers in QUO_NUMBERS."""
    wanted = {num: country for country, num in numbers.items()}
    lines = {}
    for pn in quo_pages("/phone-numbers", {}):
        country = wanted.get(digits_phone(pn.get("number") or pn.get("formattedNumber")))
        if country:
            lines[country] = pn["id"]
    return lines


def thread(line_id, phone):
    """The text thread with one candidate, oldest first: [{'id','at','from_us','text'}]."""
    msgs = []
    for m in quo_pages("/messages", {"phoneNumberId": line_id, "participants": [phone], "maxResults": 50}, limit=60):
        if (m.get("text") or "").strip():
            msgs.append({"id": m["id"], "at": iso_ts(m["createdAt"]), "from_us": m.get("direction") == "outgoing",
                         "text": " ".join(m["text"].split())})
    msgs.sort(key=lambda m: m["at"])
    return msgs


def send_text(line_id, phone, text):
    return quo("/messages", body={"content": text, "from": line_id, "to": [phone]})


# ------------------------------------------------------------------------- Claude

def build_prompt(cand, role, msgs, last_allowed, now):
    local_now = dt.datetime.fromtimestamp(now, ZoneInfo(TIMEZONES[cand["country"]])).strftime("%A %Y-%m-%d %H:%M")
    must = role.get("must_know") or []
    parts = [
        f"Current local time: {local_now}",
        f"Role: {role.get('title') or role_name(cand['group'])} ({cand['group']})",
        f"About the role: {role.get('description') or 'not provided'}",
        "What the team needs to find out:" if must else "What the team needs to find out: the candidate's relevant "
        "experience, how they would get to the properties, and their availability.",
        *[f"- {m}" for m in must],
        f"Information you may share if asked: {role.get('can_share') or 'none beyond the above'}",
        "",
        f"Candidate: {cand['name']}",
        f"Location (from CV): {cand['location'] or 'unknown'}",
        f"Car / licence (from CV): {cand['car'] or 'unknown'}",
        f"Experience (from CV): {cand['experience'] or 'unknown'}",
        f"Points the team flagged to confirm: {cand['flags'] or 'none'}",
        "",
        "Text thread so far (oldest first):" if msgs else
        "No texts yet. Write the first message: greet them by first name, say you're with Instastay following up on "
        "their application for this role, and ask your first question.",
    ]
    for m in msgs:
        stamp = dt.datetime.fromtimestamp(m["at"], ZoneInfo(TIMEZONES[cand["country"]])).strftime("%a %H:%M")
        parts.append(f"[{stamp}] {'Instastay' if m['from_us'] else first_name(cand['name'])}: {m['text']}")
    if last_allowed:
        parts += ["", "This is the last message you're allowed to send this candidate: decide now."]
    return "\n".join(parts)


def screen(client, prompt):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# --------------------------------------------------------------------------- main

def annotate(title, lines, chunk_chars=3500, max_chunks=9):
    if not os.environ.get("GITHUB_ACTIONS") or not lines:
        return
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > chunk_chars:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks[:max_chunks], 1):
        body = chunk.rstrip().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        suffix = f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""
        log(f"::notice title={title}{suffix}::{body}")


def close_vacancy(group, cands, cols, lines, now, report):
    """Someone in this vacancy is hired: thank everyone else we've texted and mark them Not Selected."""
    role = role_name(group)
    for c in cands:
        if c["stage"] in (HIRED, NOT_SELECTED, WITHDRAWN) or not c["tracking"] or not c["phone"]:
            continue
        line = lines.get(c["country"])
        if not line or not within_send_hours(c["country"], now):
            continue
        text = CLOSING_MESSAGE.format(first=first_name(c["name"]), role=role)
        report.append(f"{'WOULD CLOSE' if DRY_RUN else 'CLOSED'} {c['name']} ({group}): {text}")
        if DRY_RUN:
            continue
        send_text(line, c["phone"], text)
        update_candidate(cols, c["id"], stage=NOT_SELECTED, tracking=tracking_value(now, c["sent"] + 1))
        post_update(c["id"], f"Position filled, so I let {first_name(c['name'])} know and thanked them for applying.")


def should_screen(c):
    """Shortlisted and not yet texted, or mid-conversation. Decided or handed-over candidates are left alone."""
    if c["stage"] in HANDED_OVER or c["stage"] in (GOOD_FIT, NOT_A_FIT, WITHDRAWN, NOT_SELECTED, HIRED):
        return False
    return bool(c["tracking"]) or c["stage"] in CONTACT_STAGES


def handle_candidate(c, role, lines, cols, client, now, report):
    line = lines.get(c["country"])
    if not line or not c["phone"]:
        return
    msgs = thread(line, c["phone"])
    new_in = [m for m in msgs if not m["from_us"] and m["at"] > c["last_in"]]
    first_contact = c["stage"] in CONTACT_STAGES and not c["tracking"]
    if not first_contact and not new_in:
        return
    if new_in and msgs[-1]["from_us"]:
        # Someone already answered after their latest text (e.g. a person replied in Quo); just catch up.
        if not DRY_RUN:
            update_candidate(cols, c["id"], tracking=tracking_value(new_in[-1]["at"], c["sent"]))
        return
    if not within_send_hours(c["country"], now):
        return

    result = screen(client, build_prompt(c, role, msgs, c["sent"] + 1 >= MAX_OUR_MESSAGES, now))
    if not result:
        log(f"! Claude declined to answer for {c['name']}")
        return
    reply = result["reply"].strip()
    decision = result["decision"]
    stage = {"continue": SCREENING if msgs else CONTACTED, "good_fit": GOOD_FIT,
             "not_a_fit": NOT_A_FIT, "withdrawn": WITHDRAWN}[decision]
    if first_contact:
        stage = CONTACTED
    car = {"yes": "Confirmed by text: has car", "no": "No car"}.get(result["car"])
    label = f"{c['name']} ({c['group']}) -> {stage}"
    report.append(f"{'WOULD TEXT' if DRY_RUN else 'TEXTED'} {label}: {reply or '(no reply)'}"
                  + (f" | notes: {result['notes']}" if decision != "continue" else ""))
    if DRY_RUN:
        return
    if reply:
        send_text(line, c["phone"], reply)
    last_in = new_in[-1]["at"] if new_in else 0.0
    update_candidate(cols, c["id"], stage=stage, tracking=tracking_value(last_in or now, c["sent"] + (1 if reply else 0)),
                     notes=result["notes"], car=car)
    if decision != "continue":
        verdict = {"good_fit": "looks like a good fit", "not_a_fit": "doesn't look like a fit",
                   "withdrawn": "is no longer interested"}[decision]
        post_update(c["id"], f"Finished texting {first_name(c['name'])}: {verdict}. {result['notes']}")


def main():
    for var in ("QUO_API_KEY", "MONDAY_API_TOKEN", "ANTHROPIC_API_KEY"):
        if not os.environ.get(var):
            sys.exit(f"Missing required environment variable {var}")
    if DRY_RUN:
        log("DRY RUN: no texts are sent and monday is not changed")
    now = time.time()
    roles = load_roles(ROLES_FILE)
    lines = quo_lines(parse_quo_numbers(os.environ.get("QUO_NUMBERS")))
    log(f"Quo lines: {', '.join(lines) or 'none matched QUO_NUMBERS'}")
    cols, cands = load_candidates(VACANCIES_BOARD)
    for c in cands:
        c["country"] = vacancy_country(c["group"])

    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None)

    report, errors = [], 0
    for group in dict.fromkeys(c["group"] for c in cands):
        members = [c for c in cands if c["group"] == group]
        if any(c["stage"] == HIRED for c in members):
            close_vacancy(group, members, cols, lines, now, report)
            continue
        role = role_info(roles, group)
        for c in members:
            if not should_screen(c):
                continue
            try:
                handle_candidate(c, role, lines, cols, client, now, report)
            except (RuntimeError, OSError, anthropic.APIError) as e:
                errors += 1
                log(f"! {c['name']}: {e}")
                report.append(f"ERROR {c['name']}: {str(e)[:300]}")

    if DRY_RUN:
        stages = {}
        for c in cands:
            stages[c["stage"] or "(blank)"] = stages.get(c["stage"] or "(blank)", 0) + 1
        report.insert(0, f"Quo lines found: {', '.join(lines) or 'none'}. Candidates by stage: "
                         + ", ".join(f"{k}: {v}" for k, v in stages.items())
                         + f". Texting stages: {', '.join(CONTACT_STAGES)}.")
    log("\n".join(report) or "Nothing to do.")
    annotate(f"Candidate texts{' (dry run)' if DRY_RUN else ''}", report)
    if errors and errors == len(report):
        sys.exit(1)  # nothing worked: likely a bad token or an API change


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=HR interview run failed::{e.code}")
        raise
    except Exception as e:
        if os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=HR interview run failed::{type(e).__name__}: {str(e)[:900]}".replace("\n", " "))
        raise
