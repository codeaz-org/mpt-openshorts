"""
dry_run.py -- run the exact same research + clipping pipeline as autopilot.py,
but NEVER calls /api/social/post. Downloads every clip to ./dry_run_output/
and writes a report.md so you can actually watch the clips and judge whether
the cropping/hooks/subtitles hold up before this ever touches a real account.

Usage (after `docker compose up -d backend renderer` in your openshorts checkout):

    export GEMINI_API_KEY=...
    export YOUTUBE_API_KEY=...
    python dry_run.py                      # picks the first fresh candidate
    python dry_run.py --url <youtube-url>  # test one specific video instead
                                            # of touching posted.json / research

Nothing here writes to posted.json, so you can run it as many times as you
want while tuning sources.json without burning your "already used" list.
"""
import argparse
import os
import re
import sys

from research import find_candidates, load_json
from openshorts_client import OpenShortsClient, OpenShortsError
from attribution import build_caption, build_youtube_title, credit_line

SOURCES_PATH = os.environ.get("SOURCES_PATH", "sources.json")
POSTED_PATH = os.environ.get("POSTED_PATH", "posted.json")
OUTPUT_DIR = "dry_run_output"


def _video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def lookup_source(url):
    """Real metadata for an explicit --url, so the captions you review are the
    captions that would ship. Falls back to a clearly-labelled placeholder when
    there's no API key or the id can't be parsed -- a dry run must never
    pretend a licence was checked when it wasn't."""
    placeholder = {
        "video_id": "manual",
        "url": url,
        "title": "(manual URL -- license not re-verified, dry run only)",
        "channel_title": "unknown",
        "license": "unverified",
    }
    vid = _video_id(url)
    if not vid or not os.environ.get("YOUTUBE_API_KEY"):
        return placeholder
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", developerKey=os.environ["YOUTUBE_API_KEY"])
        items = yt.videos().list(part="status,snippet", id=vid).execute().get("items", [])
    except Exception as e:  # noqa: BLE001 -- a lookup failure must not block the dry run
        print(f"(metadata lookup failed, using placeholder: {e})")
        return placeholder
    if not items:
        return placeholder
    snippet, status = items[0]["snippet"], items[0]["status"]
    cc = status.get("license") == "creativeCommon"
    if not cc:
        print(f"WARNING: {vid} is licensed '{status.get('license')}', not creativeCommon. "
              "Dry run only -- do NOT post this one.")
    return {
        "video_id": vid,
        "url": url,
        "title": snippet.get("title"),
        "channel_title": snippet.get("channelTitle"),
        "license": "CC BY" if cc else f"NOT CC ({status.get('license')})",
    }


def pick_source(explicit_url, niche, posted):
    if explicit_url:
        return lookup_source(explicit_url)
    candidates = find_candidates(niche, posted)
    if not candidates:
        print("No fresh CC-licensed candidates found. Try --url to test a specific video.")
        sys.exit(1)
    return candidates[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Test a specific YouTube URL instead of running research")
    args = parser.parse_args()

    sources = load_json(SOURCES_PATH, {})
    niche = sources.get("niche", {})
    posted = load_json(POSTED_PATH, {"uploads": []})

    source = pick_source(args.url, niche, posted)
    print(f"Source: {source['title']}  ({source['url']})")
    if source.get("license") != "unverified":
        print(credit_line(source))

    client = OpenShortsClient()
    client.wait_ready()

    print("Submitting to OpenShorts... this can take several minutes for a long talk.")
    try:
        job_id = client.submit_job(source["url"], niche)
        result = client.wait_for_result(job_id)
    except OpenShortsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    clips = result.get("clips", [])
    if not clips:
        print("Job finished with zero clips -- source may be too short, too static, or a bad fit.")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    report_lines = [
        f"# Dry run report — {source['title']}",
        f"Source: {source['url']}",
        "",
        "Watch every clip below before trusting this niche/source config for a live run.",
        "Check specifically: does the vertical crop keep the speaker in frame, do the",
        "subtitles match the audio, does the hook text overstate what the clip shows,",
        "and does the clip make sense on its own without the rest of the talk.",
        "",
    ]

    for i, clip in enumerate(clips):
        filename = f"clip_{i}.mp4"
        dest = os.path.join(OUTPUT_DIR, filename)
        client.download_clip(job_id, clip, dest)
        title = build_youtube_title(clip, source)
        caption = build_caption(clip, source, niche.get("hashtags", ""))
        report_lines += [
            f"## Clip {i}: {title}",
            f"- File: `{dest}`",
            f"- Would-be caption:",
            "```",
            caption,
            "```",
            "",
        ]
        print(f"Saved clip {i} -> {dest}")

    report_path = os.path.join(OUTPUT_DIR, "report.md")
    with open(report_path, "w") as f:
        f.write("\n".join(report_lines))

    print(f"\nDone. {len(clips)} clip(s) saved to {OUTPUT_DIR}/, review notes in {report_path}")
    print("Nothing was posted and posted.json was not touched.")


if __name__ == "__main__":
    main()
