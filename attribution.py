"""
attribution.py -- builds the credit line CC BY legally requires on every repost.

CC BY (any version) requires, at minimum: the title of the work, the
creator/channel, a link back to the original, and a link to (or name of) the
license. This is not optional flavor text -- skip it and the repost is not
actually licensed use, license or no license on the source.
"""

LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


def credit_line(source):
    """source: one of the dicts research.py returns."""
    return (
        f"Original talk: \"{source['title']}\" by {source['channel_title']}. "
        f"{source['url']} \u2014 licensed {source.get('license', 'CC BY')} "
        f"({LICENSE_URL})"
    )


def build_caption(clip, source, hashtags):
    """clip: an item from openshorts' job result['clips'][i]."""
    hook = clip.get("video_description_for_tiktok") or clip.get("video_description_for_instagram") or ""
    credit = credit_line(source)
    parts = [p for p in [hook.strip(), credit, hashtags] if p]
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
