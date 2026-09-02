"""
attribution.py -- builds the credit line CC BY legally requires on every repost.

CC BY (any version) requires, at minimum: the title of the work, the
creator/channel, a link back to the original, and a link to (or name of) the
license. This is not optional flavor text -- skip it and the repost is not
actually licensed use, license or no license on the source.
"""

import re

# YouTube's "Creative Commons - Attribution" dropdown grants CC BY 3.0, not
# 4.0. This pointed at 4.0 and every posted clip named the wrong version --
# which matters, because naming the licence correctly IS the condition we are
# relying on to repost at all. Sources from elsewhere carry their own URL.
LICENSE_URL = "https://creativecommons.org/licenses/by/3.0/"

# A hashtag: # followed by letters/digits/underscore, not a bare '#'.
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")


def credit_line(source):
    """source: one of the dicts research.py returns.

    "Original video", not "Original talk": the sources are no longer conference
    recordings, and calling a creator's video a talk misdescribes what is being
    credited. A source may carry its own license_url when it is not a YouTube
    CC BY 3.0 upload."""
    license_url = source.get("license_url") or LICENSE_URL
    return (
        f"Original video: \"{source['title']}\" by {source['channel_title']}. "
        f"{source['url']} \u2014 licensed {source.get('license', 'CC BY 3.0')} "
        f"({license_url})"
    )


def strip_hashtags(text):
    """Drop hashtags from AI-written hook text.

    Gemini ends every hook with its own tag set, and the niche's tags were
    appended on top, so each post shipped two hashtag blocks -- ten tags, often
    contradicting each other (#opensource on a PC-build clip). The hook's prose
    is what we want; the tags are decided once, in sources.json."""
    without = _HASHTAG_RE.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", without).strip(" \t\n-|,")


def build_caption(clip, source, hashtags):
    """clip: an item from openshorts' job result['clips'][i]."""
    hook = clip.get("video_description_for_tiktok") or clip.get("video_description_for_instagram") or ""
    credit = credit_line(source)
    parts = [p for p in [strip_hashtags(hook), credit, hashtags] if p]
    return "\n\n".join(parts)


def build_youtube_title(clip, source, max_len=95):
    """Clip's own generated title, falling back to the source title. Kept
    short enough that a credit suffix doesn't get truncated by YouTube."""
    title = clip.get("video_title_for_youtube_short") or clip.get("title") or source["title"]
    return title[:max_len]


def build_youtube_description(clip, source, hashtags):
    """Same shape as build_caption -- hook, mandatory credit, hashtags --
    kept as a separate function so YouTube and TikTok copy can diverge later
    (e.g. YouTube supports chapter timestamps) without entangling the two."""
    return build_caption(clip, source, hashtags)
