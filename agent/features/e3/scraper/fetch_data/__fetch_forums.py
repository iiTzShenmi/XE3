import os
import re
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ..utils import save_json, ensure_course_folder, safe_name
from .. import config


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def _discussion_id(url):
    try:
        return (parse_qs(urlparse(url).query).get("d") or [""])[0]
    except Exception:
        return ""


def _forum_id(url):
    try:
        return (parse_qs(urlparse(url).query).get("id") or [""])[0]
    except Exception:
        return ""


def _collect_attachments(node, base_url):
    attachments = []
    for anchor in node.select("a[href*='pluginfile.php'], .attachments a[href]"):
        href = urljoin(base_url, anchor.get("href", ""))
        name = _clean_text(anchor.get_text(" ", strip=True))
        if href and name and not any(item.get("url") == href for item in attachments):
            attachments.append({"name": name, "url": href})
    return attachments


def _parse_discussion_page(session, cookies, discussion_url):
    try:
        resp = session.get(discussion_url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        return {"url": discussion_url, "error": str(e), "posts": []}

    soup = BeautifulSoup(resp.text, "html.parser")
    title_node = soup.select_one("h1, h2.discussionname, .discussionname")
    posts = []
    for idx, post in enumerate(soup.select(".forumpost, article.post, .discussion .post"), start=1):
        author = post.select_one(".author .fullname, .author, .user, .username")
        date = post.select_one(".author .date, .modified, .date")
        body = post.select_one(".content, .posting, .post-content, .no-overflow")
        attachments = _collect_attachments(post, resp.url)
        body_text = _clean_text(body.get_text("\n", strip=True)) if body else ""
        posts.append(
            {
                "index": idx,
                "author": _clean_text(author.get_text(" ", strip=True)) if author else "",
                "time": _clean_text(date.get_text(" ", strip=True)) if date else "",
                "content": body_text,
                "attachments": attachments,
            }
        )

    return {
        "discussion_id": _discussion_id(resp.url),
        "title": _clean_text(title_node.get_text(" ", strip=True)) if title_node else "",
        "url": resp.url,
        "posts": posts,
        "attachment_count": sum(len(post.get("attachments") or []) for post in posts),
    }


def fetch_forums(course_id, course_name, session, cookies):
    """Fetch forum/discussion metadata for a course."""
    course_name = safe_name(course_name)
    folder = ensure_course_folder(course_id, course_name)
    forums_file = os.path.join(folder, "forums.json")

    course_url = f"{config.E3_BASE_URL}/course/view.php?id={course_id}"
    try:
        resp = session.get(course_url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch course page for forums {course_name}: {e}")
        return

    soup = BeautifulSoup(resp.text, "html.parser")
    forum_links = []
    seen = set()
    for anchor in soup.select("a[href*='/mod/forum/view.php?id=']"):
        href = urljoin(resp.url, anchor.get("href", ""))
        name = _clean_text(anchor.get_text(" ", strip=True))
        key = (href, name)
        if href and key not in seen:
            seen.add(key)
            forum_links.append({"title": name, "url": href, "forum_id": _forum_id(href)})

    forums = []
    for forum in forum_links:
        forum_payload = dict(forum)
        discussions = []
        try:
            forum_resp = session.get(forum["url"], cookies=cookies)
            forum_resp.raise_for_status()
            forum_soup = BeautifulSoup(forum_resp.text, "html.parser")
            forum_title = forum_soup.select_one("h1, h2")
            if forum_title:
                forum_payload["title"] = _clean_text(forum_title.get_text(" ", strip=True))

            discussion_seen = set()
            for anchor in forum_soup.select("a[href*='/mod/forum/discuss.php?d=']"):
                discussion_url = urljoin(forum_resp.url, anchor.get("href", ""))
                discussion_title = _clean_text(anchor.get_text(" ", strip=True))
                discussion_id = _discussion_id(discussion_url)
                if not discussion_url or discussion_id in discussion_seen:
                    continue
                discussion_seen.add(discussion_id)
                discussion_payload = _parse_discussion_page(session, cookies, discussion_url)
                if discussion_title and not discussion_payload.get("title"):
                    discussion_payload["title"] = discussion_title
                discussions.append(discussion_payload)
        except Exception as e:
            forum_payload["error"] = str(e)

        forum_payload["discussions"] = discussions
        forum_payload["discussion_count"] = len(discussions)
        forums.append(forum_payload)

    save_json(
        forums_file,
        {
            "course_id": str(course_id),
            "course_name": str(course_name),
            "source_url": course_url,
            "forum_count": len(forums),
            "forums": forums,
        },
    )
    print(f"[+] Saved {len(forums)} forums for {course_name}")
