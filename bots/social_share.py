#!/usr/bin/env python3
"""Zero-token link sharer: new public HypeOtter YouTube videos -> X, Facebook Page, Threads via Buffer's free API.
Runs in GitHub Actions. No LLM. Captions come from approved/<slug>/manifest.json (title + pinned question) or the RSS title.
Env: BUFFER_TOKEN (repo secret), DATA_DIR (checkout of the private data repo). DRY_RUN=1 prints captions only. FORCE=1 ignores the hour window and gap.
State: social/shared.json (committed back by the workflow)."""
import json, os, re, random, sys, glob, datetime as dt
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.environ.get("DATA_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # private data checkout
CFG = json.load(open(os.path.join(ROOT, "social", "config.json")))
STATE_P = os.path.join(ROOT, "social", "shared.json")
ET_TZ = ZoneInfo("America/New_York")
DRY = os.environ.get("DRY_RUN") == "1" or not os.environ.get("BUFFER_TOKEN")
FORCE = os.environ.get("FORCE") == "1"

def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "Mozilla/5.0 HypeOtterBot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def load_state():
    if os.path.exists(STATE_P):
        return json.load(open(STATE_P))
    s = {"videos": {}, "last_post_at": None}
    for vid, plats in CFG.get("already_shared", {}).items():
        s["videos"][vid] = {p: {"ok": True, "ref": ref} for p, ref in plats.items()}
    return s

def save_state(s):
    json.dump(s, open(STATE_P, "w"), indent=1, sort_keys=True)

def rss_videos():
    out = []
    try:
        _, body = http(f"https://www.youtube.com/feeds/videos.xml?channel_id={CFG['channel_id']}")
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        for e in ET.fromstring(body).findall("a:entry", ns):
            out.append({"id": e.find("yt:videoId", ns).text, "title": e.find("a:title", ns).text,
                        "published": e.find("a:published", ns).text})
    except Exception as ex:
        print("RSS error:", ex)
    return out

def is_public(vid):
    try:
        st, body = http(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}")
        return st == 200, json.loads(body).get("title")
    except urllib.error.HTTPError:
        return False, None
    except Exception as ex:
        print("oembed error", vid, ex); return False, None

def manifests_by_id():
    up = json.load(open(os.path.join(ROOT, "yt_uploaded.json")))
    m = {}
    for slug, vid in up.items():
        if slug.startswith("_") or not isinstance(vid, str):
            continue
        p = os.path.join(ROOT, "approved", slug, "manifest.json")
        if os.path.exists(p):
            m[vid] = json.load(open(p))
    return m, [v for k, v in up.items() if not k.startswith("_") and isinstance(v, str)]

def clean_title(t):
    t = re.sub(r"#\w+", "", t or "")
    return re.sub(r"\s+", " ", t).strip(" |-")

def camel(tag):
    return "#" + "".join(w[:1].upper() + w[1:] for w in re.split(r"[\s\-]+", tag) if w)

GENERIC_Q = ["What do you think?", "Would you have done the same?", "Rate this 1 to 10.", "Who saw this coming?"]

def parts(vid, man, rss_title):
    title = clean_title((man or {}).get("title") or rss_title)
    q = (man or {}).get("pinned_comment") or ""
    q = re.sub(r"https?://\S+", "", q).replace("Full video:", "").strip()
    if "?" not in q:
        q = random.choice(GENERIC_Q)
    tags = []
    if man:
        tags = [h for h in re.findall(r"#(\w+)", man.get("description", "") + " " + man.get("title", "")) if h.lower() not in ("shorts", "short", "hypeotter")]
        tags = ["#" + h for h in dict.fromkeys(tags)]
        if len(tags) < 2:
            tags += [camel(t) for t in man.get("tags", []) if t.lower() not in ("hypeotter", "shorts")][: 2 - len(tags)]
    return title, q, tags[:3] + ["#hypeotter"]

OPEN = {
    "twitter": ["🦦", "NEW 🦦", "Otty found this 🦦", "🚨"],
    "facebook": ["Fresh upload 🦦", "New on the channel 🦦", "Otty's latest 🦦"],
    "threads": ["new one just dropped 🦦", "ok this one's wild 🦦", "fresh from Otty 🦦"],
}
CTA = {
    "twitter": ["Like, comment and subscribe 🔔", "Follow for more 🦦", "Subscribe for daily hype 🔔"],
    "facebook": ["Like, comment and follow the Page for more. Subscribe on YouTube for daily hype 🔔", "Follow for more and subscribe on YouTube 🔔"],
    "threads": ["follow for more 🦦", "like, comment and subscribe 🔔", "follow + subscribe for daily hype"],
}

def caption(platform, title, q, tags, url):
    o, c = random.choice(OPEN[platform]), random.choice(CTA[platform])
    if platform == "twitter":
        t = tags[:]
        while True:
            txt = f"{title} {o}\n\n{q}\n\n▶️ {url}\n\n{c} {' '.join(t)}"
            if len(txt) - len(url) + 23 <= 280 or not t:
                return txt
            t = t[1:] if len(t) > 1 else []
    if platform == "threads":
        return f"{o}\n\n{title}\n\n{q}\n\n▶️ {url}\n\n{c} {' '.join(tags[:2] + ['#hypeotter'] if '#hypeotter' not in tags[:2] else tags[:2])}"[:500]
    return f"{o}\n\n{title}\n\n{q}\n\n▶️ Watch: {url}\n\n{c}\n\n{' '.join(tags)}"

def gql(query):
    data = json.dumps({"query": query}).encode()
    st, body = http("https://api.buffer.com", data=data, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ["BUFFER_TOKEN"], "User-Agent": "HypeOtterBot"})
    r = json.loads(body)
    if r.get("errors"):
        raise RuntimeError(r["errors"])
    return r["data"]

def buffer_channels():
    chans = {}
    for org in gql("query { account { organizations { id name } } }")["account"]["organizations"]:
        q = 'query { channels(input: { organizationId: %s }) { id name service isQueuePaused } }' % json.dumps(org["id"])
        for ch in gql(q)["channels"]:
            chans.setdefault(ch["service"], ch)
    return chans

def buffer_post(channel_id, text, service=""):
    meta = " metadata: { facebook: { type: post } }," if service == "facebook" else ""
    q = ('mutation { createPost(input: { text: %s, channelId: %s,%s schedulingType: automatic, mode: shareNow }) '
         '{ ... on PostActionSuccess { post { id } } ... on MutationError { message } } }') % (json.dumps(text), json.dumps(channel_id), meta)
    r = gql(q)["createPost"]
    if "post" in r and r["post"]:
        return True, r["post"]["id"]
    return False, r.get("message", str(r))

NATIVE = CFG.get("native", {})

def native_owns(vid, p, man, native):
    """While native posting is on, a short with a manifest is left to the native uploader (hypeotter-media)
    until it posts there or gives up on that platform; then the link post is the fallback."""
    if not NATIVE.get("enabled") or not man or man.get("kind", "short") not in NATIVE.get("kinds", ["short"]):
        return False
    r = native.get("videos", {}).get(vid, {}).get(p, {})
    return not (r and not r.get("ok") and r.get("tries", 0) >= NATIVE.get("max_tries", 3))

def main():
    now = dt.datetime.now(ET_TZ)
    state = load_state()
    np_ = os.path.join(ROOT, "social", "native.json")
    native = json.load(open(np_)) if os.path.exists(np_) else {}
    if not FORCE:
        lo, hi = CFG["post_hours_et"]
        if not (lo <= now.hour <= hi):
            print("outside posting hours", now); save_state(state); return
        if state.get("last_post_at"):
            gap = (now - dt.datetime.fromisoformat(state["last_post_at"])).total_seconds() / 60
            if gap < CFG["min_gap_minutes"]:
                print(f"gap {gap:.0f} min < {CFG['min_gap_minutes']}"); save_state(state); return
    mans, uploaded = manifests_by_id()
    rss = rss_videos()
    rss_ids = [v["id"] for v in rss]
    rss_title = {v["id"]: v["title"] for v in rss}
    cands = list(dict.fromkeys(rss_ids + list(reversed(uploaded)) + CFG.get("extra_video_ids", [])))
    plats = CFG["platforms"]
    chans = {} if DRY else buffer_channels()
    if not DRY:
        print("buffer channels:", {k: v["name"] for k, v in chans.items()})
    done_videos = 0
    for vid in cands:
        if vid in CFG["skip_video_ids"]:
            continue
        rec = state["videos"].setdefault(vid, {})
        todo = [p for p in plats if not rec.get(p, {}).get("ok") and rec.get(p, {}).get("tries", 0) < 3]
        todo = [p for p in todo if not native_owns(vid, p, mans.get(vid), native)]
        if not todo:
            continue
        pub, otitle = is_public(vid)
        if not pub:
            continue
        title, q, tags = parts(vid, mans.get(vid), rss_title.get(vid) or otitle)
        url = (f"https://youtube.com/shorts/{vid}" if (mans.get(vid) or {}).get("kind", "short") == "short" and mans.get(vid) else f"https://youtube.com/watch?v={vid}")
        for p in todo:
            txt = caption(p, title, q, tags, url)
            print(f"--- {vid} -> {p}\n{txt}\n")
            if DRY:
                continue
            ch = chans.get(p)
            if not ch:
                print("no Buffer channel for", p); continue
            try:
                ok, ref = buffer_post(ch["id"], txt, p)
            except Exception as ex:
                ok, ref = False, str(ex)[:300]
            r = rec.setdefault(p, {})
            r["tries"] = r.get("tries", 0) + (0 if ok else 1)
            r.update({"ok": ok, "ref": ref, "at": now.isoformat(timespec="minutes")})
            print("RESULT", p, ok, ref)
        if not DRY:
            state["last_post_at"] = now.isoformat(timespec="minutes")
        done_videos += 1
        if done_videos >= CFG["max_videos_per_run"]:
            break
    if done_videos == 0:
        print("nothing new to share")
    if not DRY:
        save_state(state)

if __name__ == "__main__":
    main()
