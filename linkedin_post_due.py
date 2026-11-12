"""Post the LinkedIn rows that are due, through LinkedIn's official REST API.

Modes:
  python linkedin_post_due.py --dry-run     build every payload, check it, send nothing
  python linkedin_post_due.py --preflight   GET /v2/userinfo and upload P1's image; creates no post
  python linkedin_post_due.py               post what is due now (needs POSTING_ENABLED=true)

Secrets come from the environment only: LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_URN.
Nothing secret is ever printed.
"""
import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEDULE = os.path.join(ROOT, "linkedin_schedule.json")
TEXTS = os.path.join(ROOT, "texts.json")
POSTED = os.path.join(ROOT, "linkedin_posted.json")

API = "https://api.linkedin.com"
LINKEDIN_VERSION = "202610"  # latest per LinkedIn's versioning page, Oct 2026; supported >= 1 year
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WINDOW = dt.timedelta(hours=3)
MAX_ATTEMPTS = 3
ROUND_MINUTES = {0, 15, 30, 45}
COMMENTARY_LIMIT = 3000
IMAGE_LIMIT = 36_152_320  # LinkedIn's image size limit (bytes)

# LinkedIn "little text" reserved characters. Each is escaped with a backslash,
# except hashtags, which are written as {hashtag|\#|Word} so they stay hashtags.
RESERVED = set("\\|{}@[]()<>#*_~")
HASHTAG = re.compile(r"(?<!\w)#(\w+)")


class Unclear(Exception):
    """The request may have reached LinkedIn; the outcome is unknown."""


class Refused(Exception):
    """LinkedIn answered with a clear refusal (4xx), or nothing was sent."""


# ---------- text ----------

def escape_plain(s):
    return "".join("\\" + c if c in RESERVED else c for c in s)


def to_little_text(text):
    out, pos = [], 0
    for m in HASHTAG.finditer(text):
        out.append(escape_plain(text[pos:m.start()]))
        out.append("{hashtag|\\#|" + m.group(1) + "}")
        pos = m.end()
    out.append(escape_plain(text[pos:]))
    return "".join(out)


def visible_from_little_text(s):
    """Inverse of to_little_text, used to prove the visible text is unchanged."""
    s = re.sub(r"\{hashtag\|\\#\|(\w+)\}", r"#\1", s)
    return re.sub(r"\\(.)", r"\1", s)


# ---------- data ----------

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_posted():
    if not os.path.exists(POSTED):
        return {"posted": {}, "attempts": {}}
    d = load_json(POSTED)
    d.setdefault("posted", {})
    d.setdefault("attempts", {})
    return d


def save_posted(d):
    with open(POSTED, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write("\n")


def rows():
    sched = load_json(SCHEDULE)["posts"]
    texts = load_json(TEXTS)["texts"]
    out = []
    for r in sched:
        t = texts[r["id"]]
        text = t["linkedin_variant"] if t.get("use_variant") and t.get("linkedin_variant") else t["text"]
        when = dt.datetime.fromisoformat(f"{r['date_ist']}T{r['time_ist']}").replace(tzinfo=IST)
        out.append({"id": r["id"], "when": when, "text": text, "image": os.path.join(ROOT, r["image"]),
                    "approved_text": t["text"], "variant": text is not t["text"]})
    return out


def image_type(path):
    with open(path, "rb") as f:
        head = f.read(8)
    if head == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    raise SystemExit(f"Unknown image type: {path}")


def now_ist():
    return dt.datetime.now(IST)


# ---------- HTTP ----------

def creds():
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    urn = os.environ.get("LINKEDIN_PERSON_URN", "")
    if not token or not urn.startswith("urn:li:person:"):
        raise SystemExit("LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_URN is missing or malformed.")
    return token, urn


def request(method, url, token, body=None, data=None, headers=None, rest=True, timeout=60):
    h = {"Authorization": "Bearer " + token}
    if rest:
        h["Linkedin-Version"] = LINKEDIN_VERSION
        h["X-Restli-Protocol-Version"] = "2.0.0"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        msg = e.read()[:800].decode("utf-8", "replace")
        if 400 <= e.code < 500:
            raise Refused(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {msg}")
        raise Unclear(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {msg}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise Unclear(f"{method} {url.split('?')[0]} -> {type(e).__name__}: {e}")


def upload_image(token, urn, path):
    """Returns the image URN. Any failure here is safe to retry: no post exists yet."""
    try:
        _, _, raw = request("POST", f"{API}/rest/images?action=initializeUpload", token,
                            body={"initializeUploadRequest": {"owner": urn}})
        v = json.loads(raw)["value"]
        upload_url, image_urn = v["uploadUrl"], v["image"]
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > IMAGE_LIMIT:
            raise Refused(f"{path} is larger than LinkedIn's limit")
        request("PUT", upload_url, token, data=data, rest=False, timeout=120,
                headers={"Content-Type": image_type(path)})
    except Unclear as e:
        raise Refused(f"image upload: {e}")  # no post was attempted, so this is safe to retry
    print(f"  uploaded {os.path.basename(path)} ({image_type(path)}, {len(data)} bytes) as {image_urn}")
    # Wait until LinkedIn has processed it (usually seconds).
    status = "?"
    for _ in range(18):
        try:
            _, _, raw = request("GET", f"{API}/rest/images/{urllib.parse.quote(image_urn)}", token)
            status = json.loads(raw).get("status", "?")
        except (Refused, Unclear) as e:
            status = f"unreadable ({e})"
        if status == "AVAILABLE":
            break
        time.sleep(5)
    print(f"  image status: {status}")
    return image_urn, status


def post_payload(urn, text, image_urn):
    return {
        "author": urn,
        "commentary": to_little_text(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                         "thirdPartyDistributionChannels": []},
        "content": {"media": {"id": image_urn}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


# ---------- modes ----------

def check_row(r):
    lt = to_little_text(r["text"])
    problems = []
    if visible_from_little_text(lt) != r["text"]:
        problems.append("escaping does not round-trip")
    if len(lt) > COMMENTARY_LIMIT:
        problems.append(f"commentary {len(lt)} > {COMMENTARY_LIMIT}")
    # every reserved char outside a hashtag template must be escaped
    stripped = re.sub(r"\{hashtag\|\\#\|\w+\}", "", lt)
    stripped = re.sub(r"\\.", "", stripped)
    bad = sorted({c for c in stripped if c in RESERVED})
    if bad:
        problems.append(f"unescaped {bad}")
    if not os.path.exists(r["image"]):
        problems.append("image missing")
    elif os.path.getsize(r["image"]) > IMAGE_LIMIT:
        problems.append("image too large")
    if r["variant"]:
        problems.append("(uses an approved LinkedIn variant)")
    return lt, problems


def dry_run():
    posted = load_posted()
    rs = rows()
    now = now_ist()
    print(f"Now {now:%Y-%m-%d %H:%M} IST. {len(rs)} rows; {len(posted['posted'])} recorded in linkedin_posted.json.")
    fails = 0
    for r in rs:
        lt, problems = check_row(r)
        tags = HASHTAG.findall(r["text"])
        state = "recorded" if r["id"] in posted["posted"] else (
            "DUE" if r["when"] <= now <= r["when"] + WINDOW else "missed" if now > r["when"] + WINDOW else "waiting")
        real = [p for p in problems if not p.startswith("(")]
        fails += bool(real)
        print(f"{r['id']:4} {r['when']:%a %d %b %H:%M}  text {len(r['text']):3}  commentary {len(lt):3}  "
              f"{image_type(r['image']).split('/')[1]:4} {os.path.getsize(r['image'])/1e6:4.2f} MB  "
              f"hashtags {','.join(tags) or '-':22} {state:8} {'; '.join(problems) or 'ok'}")
    sample = next(r for r in rs if r["id"] == "P8")
    print("\nSample commentary (P8):\n" + to_little_text(sample["text"]))
    print("\nSample payload (P1), image URN is a placeholder:")
    print(json.dumps(post_payload("urn:li:person:<from secret>", rs[0]["text"], "urn:li:image:<after upload>"),
                     ensure_ascii=False, indent=1))
    if fails:
        raise SystemExit(f"{fails} row(s) failed checks")
    print("\nAll rows pass. Nothing was sent.")


def preflight():
    token, urn = creds()
    _, _, raw = request("GET", f"{API}/v2/userinfo", token, rest=False)
    info = json.loads(raw)
    if "urn:li:person:" + info.get("sub", "") != urn:
        raise SystemExit("userinfo 'sub' does not match LINKEDIN_PERSON_URN.")
    print(f"userinfo OK: token belongs to {info.get('name', '?')}, and matches LINKEDIN_PERSON_URN.")
    p1 = rows()[0]
    _, status = upload_image(token, urn, p1["image"])
    if status != "AVAILABLE":
        raise SystemExit(f"P1's image uploaded but is not AVAILABLE ({status}).")
    lt, problems = check_row(p1)
    print(f"P1 commentary {len(lt)} chars, checks: {'; '.join(problems) or 'ok'}")
    print("Preflight passed. No post was created.")


def git_pull():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        subprocess.run(["git", "pull", "-q", "--ff-only", "origin", branch], check=True)


def post_due():
    if os.environ.get("POSTING_ENABLED", "").strip().lower() != "true":
        print("Posting is off (repository variable POSTING_ENABLED is not 'true'). Nothing posted.")
        return
    token, urn = creds()
    git_pull()  # see posts recorded by any run that finished after this one was queued
    posted = load_posted()
    now = now_ist()
    due = [r for r in rows() if r["id"] not in posted["posted"] and r["when"] <= now <= r["when"] + WINDOW]
    print(f"Now {now:%Y-%m-%d %H:%M} IST. Due: {', '.join(r['id'] for r in due) or 'nothing'}.")
    failed = False
    for r in due:
        n = posted["attempts"].get(r["id"], {}).get("count", 0)
        if n >= MAX_ATTEMPTS:
            print(f"::error::{r['id']}: {n} attempts already failed; not trying again automatically.")
            failed = True
            continue
        posted["attempts"][r["id"]] = {"count": n + 1, "last": now_ist().isoformat(timespec="seconds")}
        save_posted(posted)
        print(f"{r['id']}: attempt {n + 1} of {MAX_ATTEMPTS}")
        try:
            image_urn, _ = upload_image(token, urn, r["image"])
            while now_ist().minute in ROUND_MINUTES:  # never land on :00, :15, :30 or :45
                time.sleep(5)
            st, headers, _ = request("POST", f"{API}/rest/posts", token, body=post_payload(urn, r["text"], image_urn))
            post_urn = headers.get("x-restli-id") or headers.get("X-RestLi-Id") or ""
            if st != 201 or not post_urn:
                raise Unclear(f"HTTP {st} without a post id")
            posted["posted"][r["id"]] = {
                "status": "posted", "urn": post_urn,
                "url": f"https://www.linkedin.com/feed/update/{post_urn}/",
                "posted_at_ist": now_ist().isoformat(timespec="seconds"), "image": image_urn}
            print(f"{r['id']}: posted {posted['posted'][r['id']]['url']}")
        except Refused as e:
            posted["attempts"][r["id"]]["error"] = str(e)[:500]
            print(f"::error::{r['id']}: refused, may retry within the window: {e}")
            failed = True
        except Unclear as e:
            posted["posted"][r["id"]] = {
                "status": "unconfirmed", "note": f"{e}"[:500],
                "at_ist": now_ist().isoformat(timespec="seconds")}
            print(f"::error::{r['id']}: outcome unclear; marked unconfirmed and never retried automatically. "
                  f"Check the profile. {e}")
            failed = True
        save_posted(posted)
    if failed:
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--preflight", action="store_true")
    a = ap.parse_args()
    if a.dry_run:
        dry_run()
    elif a.preflight:
        preflight()
    else:
        post_due()


if __name__ == "__main__":
    main()
