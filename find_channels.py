"""
find_channels.py -- discover CC-BY channels worth adding to sources.json.

Why this exists: every wrong entry in `cc_channels` so far was written from
search metadata rather than from what the channel actually publishes. "Humor
Studios" was noted as "Security/hacker story format" and is an AskReddit
storytime channel -- it supplied six consecutive posts about undiagnosed
medical conditions. "CampusX" was noted as "AI coding and dev education" and
is Hindi-language lecture content, which is the exact opposite of the niche's
stated purpose (creators who already edit for attention).

So this measures instead of guessing. It runs the niche's own search queries
with YouTube's CC filter, re-verifies every hit's licence per video the way
research.py does, drops anything the topic filter rejects, and then GROUPS
what survives by channel -- so a channel's score is "how many long, on-topic,
genuinely CC-licensed videos does it actually have", not "did it show up once".

It does NOT edit sources.json. Adding a channel is still a human decision:
read the sample titles it prints, open the channel, and confirm the licence
policy on the About page. This narrows the field, it does not vet.

Usage:
    export YOUTUBE_API_KEY=...
    python find_channels.py
    python find_channels.py --query "neovim config" --query "rust tutorial"
    python find_channels.py --min-videos 5 --json

No API key? It falls back to the OAuth refresh token this repo already uses to
upload, which can come from another project's .env:

    python find_channels.py --env-file ../mpt/.env

Quota: each query costs 100 units (search.list) plus 1 per 50 videos checked.
The default 10k/day key affords roughly 90 queries.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from googleapiclient.discovery import build

from research import (SOURCES_PATH, load_json, iso8601_duration_to_seconds,
                      topic_verdict)


def load_env_file(path):
    """Read KEY=VALUE lines into os.environ without overwriting what's set.

    Same parser get_youtube_token.py already uses. Exists so credentials can
    come from a .env this repo does not own -- the OAuth client and the CodeAZ
    refresh token live in the sibling `mpt` project, and re-minting a second
    copy of a token that already exists is pure ceremony.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return False
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


def youtube_client(niche):
    """An API key if there is one, otherwise the niche's OAuth refresh token.

    search.list and videos.list are public reads: an API key is the usual way
    in, but an OAuth credential authorizes them too, and this repo already
    holds one for the channel it posts to. That matters because the API key
    and the refresh token tend to live in different places -- the key in CI
    secrets, the token on the machine that minted it -- and the discovery tool
    is useless if it can only run where the key is.
    """
    api_key = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if api_key:
        print("auth: YOUTUBE_API_KEY", file=sys.stderr)
        return build("youtube", "v3", developerKey=api_key)

    suffix = re.sub(r"[^A-Za-z0-9]", "",
                    niche.get("env_suffix") or niche.get("id") or "").upper()
    client_id = (os.environ.get(f"YT_CLIENT_ID_{suffix}")
                 or os.environ.get("YT_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get(f"YT_CLIENT_SECRET_{suffix}")
                     or os.environ.get("YT_CLIENT_SECRET") or "").strip()
    refresh_token = (os.environ.get(f"YT_REFRESH_TOKEN_{suffix}") or "").strip()

    if not (client_id and client_secret and refresh_token):
        print("ERROR: set YOUTUBE_API_KEY, or provide OAuth credentials "
              f"(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN_{suffix}) -- "
              "pass --env-file to read them from another project's .env.",
              file=sys.stderr)
        sys.exit(1)

    from google.oauth2.credentials import Credentials
    # The token this repo mints carries the upload scope. Public reads are
    # normally granted alongside it; if YouTube answers 403
    # insufficientPermissions, re-mint with youtube.readonly instead.
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/youtube.upload"],
    )
    print(f"auth: OAuth refresh token (YT_REFRESH_TOKEN_{suffix})", file=sys.stderr)
    return build("youtube", "v3", credentials=creds)


def search_cc(youtube, query, duration, order, per_query):
    """One CC-filtered search page. Returns video ids."""
    resp = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        videoLicense="creativeCommon",
        videoDuration=duration,
        order=order,
        maxResults=per_query,
    ).execute()
    return [i["id"]["videoId"] for i in resp.get("items", [])]


def fetch_videos(youtube, video_ids):
    """videos.list with statistics -- the licence re-check AND the view counts
    that separate a channel people watch from a channel that merely exists."""
    out = {}
    for i in range(0, len(video_ids), 50):
        resp = youtube.videos().list(
            part="status,snippet,contentDetails,statistics",
            id=",".join(video_ids[i:i + 50]),
        ).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", action="append", default=[],
                        help="Extra search query (repeatable). Niche queries run too.")
    parser.add_argument("--only-my-queries", action="store_true",
                        help="Skip sources.json search_queries, use only --query.")
    parser.add_argument("--min-videos", type=int, default=3,
                        help="Only report channels with at least this many qualifying videos.")
    parser.add_argument("--per-query", type=int, default=50,
                        help="Results per search (max 50).")
    parser.add_argument("--json", action="store_true",
                        help="Print a cc_channels block ready to paste into sources.json.")
    parser.add_argument("--env-file", action="append", default=[],
                        help="Read credentials from this .env too (repeatable). "
                             "./.env is always read when present.")
    args = parser.parse_args()

    for path in [".env"] + args.env_file:
        if load_env_file(path):
            print(f"loaded {path}", file=sys.stderr)

    niche = load_json(SOURCES_PATH, {}).get("niche", {})
    known = {c["channel_id"]: c.get("name", "?") for c in niche.get("cc_channels", [])}
    min_seconds = niche.get("min_source_seconds", 600)
    duration = niche.get("video_duration", "medium")

    queries = list(args.query)
    if not args.only_my_queries:
        queries += niche.get("search_queries", [])
    if not queries:
        print("No queries. Add search_queries to sources.json or pass --query.",
              file=sys.stderr)
        sys.exit(1)

    youtube = youtube_client(niche)

    # Two orderings per query on purpose. viewCount finds the channels people
    # actually watch; relevance finds the ones that are on topic but smaller,
    # and the CC pool is thin enough that dropping those loses real candidates.
    ids = []
    for q in queries:
        for order in ("viewCount", "relevance"):
            try:
                ids += search_cc(youtube, q, duration, order, args.per_query)
            except Exception as e:  # noqa: BLE001 -- one bad query must not end the sweep
                print(f"  query {q!r} ({order}) failed: {e}", file=sys.stderr)
        print(f"searched {q!r} -- {len(ids)} ids so far", file=sys.stderr)

    ids = list(dict.fromkeys(ids))
    print(f"\n{len(ids)} unique candidates; re-verifying licence per video...",
          file=sys.stderr)
    videos = fetch_videos(youtube, ids)

    channels = defaultdict(lambda: {"name": "", "videos": [], "views": []})
    rejected = {"licence": 0, "short": 0, "off_topic": 0}
    for vid, item in videos.items():
        if item.get("status", {}).get("license") != "creativeCommon":
            rejected["licence"] += 1
            continue
        seconds = iso8601_duration_to_seconds(item["contentDetails"]["duration"])
        if seconds < min_seconds:
            rejected["short"] += 1
            continue
        ok, _why = topic_verdict(item, niche)
        if not ok:
            rejected["off_topic"] += 1
            continue
        snippet = item["snippet"]
        entry = channels[snippet["channelId"]]
        entry["name"] = snippet.get("channelTitle", "")
        entry["videos"].append((snippet.get("title", ""), seconds))
        entry["views"].append(int(item.get("statistics", {}).get("viewCount") or 0))

    print(f"rejected: {rejected['licence']} not actually CC, "
          f"{rejected['short']} under {min_seconds}s, "
          f"{rejected['off_topic']} off topic\n", file=sys.stderr)

    ranked = sorted(
        ((cid, c) for cid, c in channels.items() if len(c["videos"]) >= args.min_videos),
        key=lambda kv: (len(kv[1]["videos"]), sum(kv[1]["views"])),
        reverse=True,
    )

    if not ranked:
        print("Nothing cleared the bar. Try more --query terms or --min-videos 1.")
        return

    for cid, c in ranked:
        median_views = sorted(c["views"])[len(c["views"]) // 2]
        mark = f"  [ALREADY LISTED as {known[cid]!r}]" if cid in known else ""
        print(f"\n{c['name']}  ({cid}){mark}")
        print(f"  {len(c['videos'])} qualifying CC videos, median {median_views:,} views")
        # Titles are the point: this is where "security/hacker story format"
        # would have been caught as AskReddit before it ever posted.
        for title, seconds in sorted(c["videos"], key=lambda v: -v[1])[:5]:
            print(f"    - [{seconds // 60:>3}m] {title}")

    if args.json:
        print("\n--- candidates not already in sources.json ---")
        print(json.dumps([
            {"channel_id": cid,
             "name": c["name"],
             "note": f"UNVERIFIED -- {len(c['videos'])} CC videos found by find_channels.py. "
                     f"Watch one and check the About page before trusting this."}
            for cid, c in ranked if cid not in known
        ], indent=2))


if __name__ == "__main__":
    main()
