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

Quota: each query costs 100 units (search.list) plus 1 per 50 videos checked.
The default 10k/day key affords roughly 90 queries.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

from googleapiclient.discovery import build

from research import (SOURCES_PATH, load_json, iso8601_duration_to_seconds,
                      topic_verdict)


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
    args = parser.parse_args()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YOUTUBE_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

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

    youtube = build("youtube", "v3", developerKey=api_key)

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
