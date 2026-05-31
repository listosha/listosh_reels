# -*- coding: utf-8 -*-
"""Cloud reel publisher — runs inside GitHub Actions (PC-off), mirrors the carousel workflow.

One run = ONE due reel → Instagram Reel (video_url + designed cover_url) + YouTube Short.
Picks the earliest pending entry in schedule.json whose slot time has already passed (never early,
only late), marks it published, and the workflow commits schedule.json back.

VK is NOT here on purpose: its video token is IP-bound + 24h and GH runners change IP → handled
separately (local PC / VPS).

Repo layout (public): videos/<slug>.mp4, thumbs/<slug>.png, schedule.json, this script.
Env:
  INSTAGRAM_ACCESS_TOKEN   IG token (GitHub secret; same as carousels)
  IG_USER_ID               default 17841403939108726
  REPO_RAW                 e.g. https://raw.githubusercontent.com/<user>/<repo>/main
  PLATFORMS                "ig,yt" (default)
  YT_TOKEN_JSON_PATH       token.json with refresh_token (written by the workflow from a secret)
  YT_CLIENT_SECRET_PATH    client_secret.json (written from a secret)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

GRAPH = "https://graph.facebook.com/v21.0"
IG_USER_ID = os.environ.get("IG_USER_ID", "17841403939108726")
REPO_RAW = os.environ.get("REPO_RAW", "").rstrip("/")
PLATFORMS = [p.strip() for p in os.environ.get("PLATFORMS", "ig,yt").split(",") if p.strip()]
MSK = timezone(timedelta(hours=3))


def due_entry(schedule):
    """Earliest pending whose slot already passed (drains strictly in order, never early)."""
    now = datetime.now(MSK)
    best_i, best_dt = None, None
    for i, e in enumerate(schedule):
        if e.get("status") != "pending":
            continue
        dt = datetime.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
        if dt > now:
            continue
        if best_dt is None or dt < best_dt:
            best_i, best_dt = i, dt
    return best_i


def publish_ig(slug, caption):
    token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
    video_url = f"{REPO_RAW}/videos/{slug}.mp4"
    cover_url = f"{REPO_RAW}/thumbs/{slug}.png"
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", params={
        "media_type": "REELS", "video_url": video_url, "cover_url": cover_url,
        "caption": caption, "share_to_feed": "true", "access_token": token}, timeout=120).json()
    if "id" not in r:
        raise RuntimeError("IG container: " + json.dumps(r, ensure_ascii=False))
    cid = r["id"]
    # Reels processing can take a while when IG fetches the video by URL
    for _ in range(60):
        time.sleep(5)
        s = requests.get(f"{GRAPH}/{cid}", params={
            "fields": "status_code", "access_token": token}, timeout=30).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError("IG processing ERROR: " + json.dumps(s, ensure_ascii=False))
    else:
        raise RuntimeError("IG container not ready in time")
    pub = requests.post(f"{GRAPH}/{IG_USER_ID}/media_publish", params={
        "creation_id": cid, "access_token": token}, timeout=60).json()
    if "id" not in pub:
        raise RuntimeError("IG publish: " + json.dumps(pub, ensure_ascii=False))
    return "https://www.instagram.com/reel/" + pub["id"]


def publish_yt(slug, title, caption, tags):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials.from_authorized_user_file(
        os.environ["YT_TOKEN_JSON_PATH"],
        ["https://www.googleapis.com/auth/youtube.upload"])
    if not creds.valid:
        creds.refresh(Request())               # headless refresh via refresh_token
    yt = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {"title": title[:95], "description": caption + "\n\n#Shorts",
                    "tags": tags, "categoryId": "22"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(f"videos/{slug}.mp4", chunksize=8 * 1024 * 1024, resumable=True,
                            mimetype="video/*")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    thumb = f"thumbs/{slug}.png"
    if os.path.exists(thumb):
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
    return "https://youtu.be/" + vid


def main():
    with open("schedule.json", encoding="utf-8") as f:
        schedule = json.load(f)
    i = due_entry(schedule)
    if i is None:
        print("Нет созревших pending-роликов. Выходим.")
        return 0
    e = schedule[i]
    print(f"Публикую {e['slug']} (слот {e['date']} {e['time']}) на: {','.join(PLATFORMS)}")

    results, failed = {}, []
    if "ig" in PLATFORMS:
        try:
            results["instagram"] = publish_ig(e["slug"], e["caption"])
            print("  ✓ IG:", results["instagram"])
        except Exception as ex:
            failed.append(f"ig: {ex}"); print("  ✗ IG:", ex)
    if "yt" in PLATFORMS:
        try:
            results["youtube"] = publish_yt(e["slug"], e["title"], e["caption"], e.get("tags", []))
            print("  ✓ YT:", results["youtube"])
        except Exception as ex:
            failed.append(f"yt: {ex}"); print("  ✗ YT:", ex)

    # mark published only if at least one platform succeeded; record per-platform result
    if results:
        e["status"] = "published"
        e["results"] = results
        e["published_at"] = datetime.now(MSK).isoformat()
        if failed:
            e["partial_errors"] = failed
        with open("schedule.json", "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)
        print("schedule.json обновлён.")
    if failed and not results:
        sys.exit(1)   # nothing posted → fail the run so it retries next trigger
    return 0


if __name__ == "__main__":
    sys.exit(main())
