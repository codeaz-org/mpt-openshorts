"""
research.py -- find long-form YouTube videos we're actually allowed to reclip.

Two-step verification, not one:
  1. search.list(videoLicense="creativeCommon") to narrow the candidate pool.
  2. videos.list(...).status.license re-checked per video, because the search
     filter reflects what the uploader flagged and uploaders get this wrong in
     both directions. Only videos that pass BOTH steps are used.

Two arms, and the ranking between them has been inverted:

  1. OPEN SEARCH across all of YouTube under the CC filter, a random sample of
     search_queries each run, two orderings per query. This is the primary arm
     and it runs whether or not a single channel is listed anywhere.
  2. Channel arms: the seeds in sources.json, plus channels DISCOVERED by arm 1
     that have since produced clips which survived the clip filter.

The allowlist used to be arm 1 and open search the "lower trust" fallback.
That was backwards, and the output proved it: all three channels removed from
the allowlist so far -- an AskReddit storytime channel, a Hindi lecture
channel, and a PC channel that publishes sentimental stories -- were vetted by
a human and listed, and every post that had to be deleted came from one of
them. Vetting a channel is a one-time judgement about a publisher, and it goes
stale the moment that publisher changes format. The licence re-verification,
the topic filter and the clip filter are evaluated fresh, per video, and they
apply identically to both arms. So the net finds sources and the per-video
gates qualify them; being listed buys a guaranteed search arm and nothing else.

Ordering within the open arm: viewCount finds what people actually watch (the
clipper amplifies pacing a source already has and cannot invent it, so an
unwatched CC upload is rarely worth clipping), and date reaches material the
viewCount ranking will never surface. Queries are sampled rather than all run,
because a fixed query set returns the same top results every day, posted.json
then rejects them as already used, and the pool goes sterile within days.

The pool maintains itself from there: record_outcome tallies what each
channel's clips did, promoted_channel_ids gives an arm to channels whose clips
survive, and blocked_channel_ids cuts off channels whose clips keep getting
rejected. That last one is the check that should have removed Humor Studios
and KristoferYee -- automatically, from their own output, instead of after a
human watched bad videos go out and edited a config file.

Then a THIRD check, added after watching what the first two let through: the
topic filter (topic_verdict). Licensing and channel vetting between them never
ask whether a video is about the subject the channel exists for, and both of
the answers they do give go stale -- a vetted channel changes format, and the
keyword arm reaches channels nobody vetted. Six consecutive posts about
undiagnosed medical conditions went out on a channel called CodeAZ Tech Shorts
because every check upstream of this one passed.

Requires: YOUTUBE_API_KEY (a plain API key is enough -- no OAuth needed,
this only reads public search/videos endpoints).
"""
import json
import os
import random
import re
import sys
from googleapiclient.discovery import build

SOURCES_PATH = os.environ.get("SOURCES_PATH", "sources.json")
POSTED_PATH = os.environ.get("POSTED_PATH", "posted.json")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def already_used(video_id, posted):
    return any(u.get("source_video_id") == video_id for u in posted.get("uploads", []))


def verify_license(youtube, video_ids):
    """Authoritative check: videos.list status.license, not the search filter."""
    if not video_ids:
        return {}
    out = {}
    # videos.list caps the id filter at 50 -- more than that comes back as
    # HTTP 400 invalidFilters, so page through in chunks.
    for i in range(0, len(video_ids), 50):
        resp = youtube.videos().list(
            part="status,snippet,contentDetails",
            id=",".join(video_ids[i:i + 50]),
        ).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item
    return out


def iso8601_duration_to_seconds(duration):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def channel_stats(posted):
    """Per-channel tallies autopilot writes after each run. See record_outcome."""
    return (posted.get("channels") or {})


def blocked_channel_ids(niche, posted):
    """Channels this run must not draw from.

    Two sources: manual, permanent entries in sources.json, and automatic ones
    earned in posted.json by a channel whose clips keep getting rejected and
    never get kept. The automatic half is the point -- Humor Studios and
    KristoferYee were both removed by hand, after a human watched bad clips go
    out. A channel's own output is the evidence, and it is available without
    anyone watching anything.
    """
    blocked = set(niche.get("blocked_channels") or [])
    limit = (niche.get("discovery") or {}).get("auto_block_after_rejects", 4)
    for cid, st in channel_stats(posted).items():
        if st.get("status") == "blocked":
            blocked.add(cid)
        elif st.get("clips_rejected", 0) >= limit and not st.get("clips_kept"):
            blocked.add(cid)
    return blocked


def promoted_channel_ids(niche, posted, blocked):
    """Discovered channels that have earned a search arm of their own.

    A channel reaches this list by having produced clips that survived the clip
    filter -- which is a claim about output, evaluated fresh, rather than about
    a note somebody wrote once. Ordered by clips kept so the ceiling drops the
    weakest first.
    """
    cfg = niche.get("discovery") or {}
    need = cfg.get("promote_after_keeps", 2)
    ceiling = cfg.get("max_promoted_channels", 12)
    seeds = {c["channel_id"] for c in niche.get("cc_channels", [])}
    earned = [
        (cid, st) for cid, st in channel_stats(posted).items()
        if cid not in blocked and cid not in seeds
        and st.get("clips_kept", 0) >= need
    ]
    earned.sort(key=lambda kv: kv[1].get("clips_kept", 0), reverse=True)
    return [cid for cid, _ in earned[:ceiling]]


def record_outcome(posted, source, kept, rejected):
    """Tally one run's result against the source's channel, in posted.json.

    This is what turns the pipeline from "a fixed list a human curates" into
    something that maintains its own pool: channels that produce usable clips
    earn more of the search budget, channels that produce rejects lose access
    entirely, and neither outcome needs anyone to notice and edit a file.
    """
    cid = source.get("channel_id")
    if not cid:
        return
    channels = posted.setdefault("channels", {})
    st = channels.setdefault(cid, {
        "name": source.get("channel_title") or "",
        "sources_used": 0, "clips_kept": 0, "clips_rejected": 0,
    })
    st["name"] = source.get("channel_title") or st.get("name") or ""
    st["sources_used"] = st.get("sources_used", 0) + 1
    st["clips_kept"] = st.get("clips_kept", 0) + kept
    st["clips_rejected"] = st.get("clips_rejected", 0) + rejected
    st["last_seen"] = source.get("_ts") or st.get("last_seen")
    return st


def interleave(arms):
    """One id from each arm in turn, deduped, order preserved.

    autopilot.py posts candidates[0] and nothing else, so the ORDER of this
    list is the entire editorial decision the pipeline makes. Concatenated
    arms made that decision badly: a channel arm returns up to 25 videos, so
    the first arm owned every slot a run could reach and the keyword arm --
    appended after five channel arms -- was unreachable in practice. Taking
    one from each arm per round means the top of the list is one video from
    each distinct source, which is what "pick something fresh" should mean.
    """
    out = []
    seen = set()
    for i in range(max((len(a) for a in arms), default=0)):
        for arm in arms:
            if i >= len(arm):
                continue
            vid = arm[i]
            if vid in seen:
                continue
            seen.add(vid)
            out.append(vid)
    return out


def _term_re(term):
    """A term matches on word boundaries, so 'ai' does not fire inside
    'explain' and 'r/' still matches literally."""
    return re.compile(r"(?<!\w)" + re.escape(term.strip().lower()) + r"(?!\w)")


def topic_verdict(item, niche):
    """(ok, reason) -- is this video about what the channel is about?

    Nothing upstream asked this question. The licence check answers "may we
    repost it" and the channel allowlist answers "does this uploader license
    CC", and neither answers "is it on topic". So an AskReddit channel listed
    with the note "security/hacker story format" supplied six straight posts
    about undiagnosed medical conditions to a channel called CodeAZ Tech
    Shorts (3-sep and 5-sep-2026 runs). A vetted channel drifts, and the
    keyword arm reaches channels nobody vetted at all.

    exclude_terms are checked FIRST and beat everything: a storytime video
    whose description happens to say "tech" is still a storytime video.

    Language is checked as its own axis, because topic keywords cannot see it:
    a Hindi machine-learning lecture matches "machine learning" perfectly and
    is still unusable on an English shorts channel. Videos that declare no
    language PASS -- the field is optional and widely unset, so rejecting on
    absence would throw away most of the pool to catch a few.
    """
    snippet = item.get("snippet") or {}
    allowed = [l.lower() for l in niche.get("allowed_languages", [])]
    if allowed:
        declared = (snippet.get("defaultAudioLanguage")
                    or snippet.get("defaultLanguage") or "")
        # "en-GB" and "en" are the same language for this purpose.
        primary = declared.split("-")[0].lower()
        if primary and primary not in allowed:
            return False, f"language {declared!r}"

    hay = " ".join([
        snippet.get("title") or "",
        (snippet.get("description") or "")[:600],
        " ".join(snippet.get("tags") or []),
    ]).lower()

    for term in niche.get("exclude_terms", []):
        if _term_re(term).search(hay):
            return False, f"excluded by {term!r}"

    topics = niche.get("topic_terms", [])
    if not topics:
        return True, "no topic_terms configured"
    hits = [t for t in topics if _term_re(t).search(hay)]
    if not hits:
        return False, "no topic term matched"
    return True, ", ".join(hits[:3])


def clip_verdict(clip, niche):
    """(ok, reason) -- the same topic test, applied to what Gemini wrote ABOUT
    the clip rather than to the source video's metadata.

    Source metadata is a weak proxy and it fails in a specific direction: a
    channel's description says what the channel is, not what this video is.
    "Her Brother Won't Let Her Play on the Computer" (KristoferYee, 6-sep-2026)
    carries a hardware description because it is a PC channel, so the source
    filter passes it -- and the three clips cut from it were titled "I realized
    I was a terrible brother", "Why I gave a stranger a gaming PC" and "The 1%
    rule that changed my life". All three had to be deleted by hand.

    The generated title and hook are the honest signal, because they describe
    the 40 seconds actually being posted. This runs on them AFTER clipping, so
    it costs a wasted render rather than a wasted post -- the cheaper mistake.

    Only exclude_terms apply. Requiring a topic term here would reject a clip
    whose hook is a legitimate cold open ("I deleted the wrong directory"), and
    the source has already been checked for topic by this point.
    """
    text = " ".join([
        clip.get("video_title_for_youtube_short") or "",
        clip.get("video_description_for_tiktok") or "",
        clip.get("viral_hook_text") or "",
    ]).lower()
    if not text.strip():
        return True, "no clip text to judge"
    for term in niche.get("exclude_terms", []):
        if _term_re(term).search(text):
            return False, f"excluded by {term!r}"
    for term in niche.get("clip_exclude_terms", []):
        if _term_re(term).search(text):
            return False, f"excluded by {term!r}"
    return True, "ok"


def find_candidates(niche, posted, max_results=25):
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YOUTUBE_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    youtube = build("youtube", "v3", developerKey=api_key)
    min_seconds = niche.get("min_source_seconds", 600)
    duration = niche.get("video_duration", "medium")

    # Each search becomes its own ARM, and the arms are interleaved below.
    # Concatenating them meant one channel's whole recent catalogue sat ahead of
    # every other arm, and autopilot only ever takes candidates[0] -- so a run
    # was always three clips from a single channel, and the keyword arm's
    # results, appended last, could never be reached at all.
    arms = []

    blocked = blocked_channel_ids(niche, posted)
    if blocked:
        print(f"{len(blocked)} channel(s) blocked this run.", file=sys.stderr)

    # 1) OPEN SEARCH -- the primary arm, and it runs whether or not a single
    #    channel is listed anywhere. This used to be the "secondary, lower
    #    trust" arm behind a human-vetted allowlist, and that ranking had it
    #    exactly backwards: all three channels removed from the allowlist so far
    #    were human-vetted and listed, and every bad post came from one of them.
    #    A vetted channel is a stale one-time judgement about a publisher; the
    #    licence re-verification, topic filter and clip filter are evaluated
    #    fresh, per video, on both arms alike. So the net is what finds sources
    #    and the per-video gates are what qualify them.
    #
    #    Queries are SAMPLED, not all run: the full list every day costs quota
    #    and returns the same top results, which posted.json then rejects as
    #    already used, so a fixed set of queries goes sterile within days.
    queries = list(niche.get("search_queries", []))
    per_run = niche.get("queries_per_run", 8)
    if len(queries) > per_run:
        queries = random.sample(queries, per_run)

    for q in queries:
        # Two orderings per query. viewCount finds what people actually watch
        # (the clipper amplifies pacing a source already has and cannot invent
        # it); date reaches material the viewCount ranking will never surface,
        # which is the half that keeps the pool from going stale.
        for order in ("viewCount", "date"):
            try:
                resp = youtube.search().list(
                    part="snippet",
                    q=q,
                    type="video",
                    videoLicense="creativeCommon",
                    videoDuration=duration,
                    order=order,
                    maxResults=10,
                ).execute()
            except Exception as e:  # noqa: BLE001 -- one bad query must not end the run
                print(f"query {q!r} ({order}) failed: {e}", file=sys.stderr)
                continue
            arms.append([i["id"]["videoId"] for i in resp.get("items", [])
                         if i["snippet"].get("channelId") not in blocked])

    # 2) Channel arms: the seeds from sources.json, plus channels DISCOVERED by
    #    arm 1 that have since produced clips which survived the clip filter.
    #    A seed is a guaranteed search arm and nothing more -- it buys no trust
    #    that the per-video gates do not re-establish every run.
    channel_ids = [c["channel_id"] for c in niche.get("cc_channels", [])
                   if c["channel_id"] not in blocked]
    promoted = promoted_channel_ids(niche, posted, blocked)
    if promoted:
        print(f"{len(promoted)} promoted channel(s) earning an arm this run.",
              file=sys.stderr)
    channel_ids += promoted

    # Rotate which channel is asked first. autopilot always takes candidates[0],
    # so a fixed list order means the first channel is mined until it runs dry
    # and the rest never get a turn -- with FOSDEM listed first, every single
    # run picked a FOSDEM talk. Rotating spreads it across the pool.
    if channel_ids:
        k = random.randrange(len(channel_ids))
        channel_ids = channel_ids[k:] + channel_ids[:k]

    for cid in channel_ids:
        try:
            resp = youtube.search().list(
                part="snippet",
                channelId=cid,
                type="video",
                videoLicense="creativeCommon",
                videoDuration=duration,
                order="date",
                maxResults=max_results,
            ).execute()
        except Exception as e:  # noqa: BLE001
            print(f"channel {cid} search failed: {e}", file=sys.stderr)
            continue
        arms.append([item["id"]["videoId"] for item in resp.get("items", [])])

    candidate_ids = interleave(arms)
    candidate_ids = [v for v in candidate_ids if not already_used(v, posted)]
    if not candidate_ids:
        return []

    verified = verify_license(youtube, candidate_ids)

    results = []
    for vid in candidate_ids:
        item = verified.get(vid)
        if not item:
            continue
        if item.get("status", {}).get("license") != "creativeCommon":
            # The authoritative flag disagrees with the search filter -- skip.
            continue
        if item["snippet"].get("channelId") in blocked:
            continue
        seconds = iso8601_duration_to_seconds(item["contentDetails"]["duration"])
        if seconds < min_seconds:
            continue
        on_topic, why = topic_verdict(item, niche)
        if not on_topic:
            print(f"Skipping {vid}: off topic ({why}) -- "
                  f"{(item['snippet'].get('title') or '')!r}", file=sys.stderr)
            continue
        snippet = item["snippet"]
        # OpenShorts names the downloaded file after the video title and hands
        # that name to the Gemini Files API, which puts it in an HTTP header --
        # headers are ASCII-only, so a title carrying an en-dash or an accent
        # fails clip detection and kills the whole job.
        #
        # This is a backstop, not the fix: skipping these throws away most of a
        # conference catalogue (measured: 49 of 50 recent CC-licensed Godot
        # uploads are "Talk - Speaker - GodotCon 2026" with en-dashes). The
        # real fix is in the backend, which uploads through an ASCII-named
        # hardlink. Set OPENSHORTS_ASCII_SAFE=1 when you're pointed at a
        # backend carrying that fix and this guard stands down.
        title = snippet.get("title") or ""
        ascii_safe_backend = (os.environ.get("OPENSHORTS_ASCII_SAFE") or "").strip().lower() in ("1", "true", "yes")
        if not title.isascii() and not ascii_safe_backend:
            offending = sorted({c for c in title if not c.isascii()})
            print(f"Skipping {vid}: non-ASCII title breaks Gemini upload "
                  f"({''.join(offending)!r} in {title!r}). Set OPENSHORTS_ASCII_SAFE=1 "
                  f"if your backend has the hardlink fix.", file=sys.stderr)
            continue
        results.append({
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": snippet.get("title"),
            "channel_title": snippet.get("channelTitle"),
            "channel_id": snippet.get("channelId"),
            "duration_seconds": seconds,
            "license": "CC BY 3.0",
            "topic_match": why,
        })

    return results


def main():
    sources = load_json(SOURCES_PATH, {})
    niche = sources.get("niche", {})
    posted = load_json(POSTED_PATH, {"uploads": []})

    max_candidates = niche.get("max_candidates_per_run", 5)
    candidates = find_candidates(niche, posted)[:max_candidates]

    print(json.dumps(candidates, indent=2))
    return candidates


if __name__ == "__main__":
    main()
