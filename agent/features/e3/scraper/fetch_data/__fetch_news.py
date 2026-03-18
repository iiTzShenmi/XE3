import os
import requests
from bs4 import BeautifulSoup
from ..utils import save_json, load_json, ensure_course_folder, safe_name
from .. import config

def fetch_news(course_id, course_name, session, cookies):
    """Fetch news for a specific course."""
    course_name = safe_name(course_name)
    folder = ensure_course_folder(course_id, course_name)
    news_file = os.path.join(folder, "news.json")
    news_data = []

    url = f"{config.E3_NEWS_URL}?id={course_id}&lang=zh_tw"
    try:
        resp = session.get(url, cookies=cookies)
        resp.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to fetch news page for {course_name}: {e}")
        return
    
    soup = BeautifulSoup(resp.text, "html.parser")

    updated = False
    for item in soup.select("li.post"):
        date_tag = item.select_one("div.date")
        title_tag = item.select_one("div.name")
        link_tag = item.select_one("div.info a")

        if not (date_tag and title_tag and link_tag):
            continue

        date = date_tag.get_text(strip=True)
        title = title_tag.get_text(strip=True)
        link = link_tag["href"]

        # Skip if already stored
        '''if any(n.get("title") == title and n.get("date") == date for n in news_data):
            continue'''

        # ---- Fetch full content ----
        full_content = ""
        comments = []
        try:
            detail_resp = session.get(link, cookies=cookies)
            detail_soup = BeautifulSoup(detail_resp.text, "html.parser")

            # main content (often inside div.news-content or similar)
            content_div = detail_soup.select_one("div.news-content") or detail_soup.select_one("div.content")
            if content_div:
                full_content = content_div.get_text("\n", strip=True)

            # comments (if available)
            for com in detail_soup.select("div.comment"):
                author = com.select_one(".user, .author")
                time = com.select_one(".time, .date")
                body = com.select_one(".content, .text")
                comments.append({
                    "author": author.get_text(strip=True) if author else "",
                    "time": time.get_text(strip=True) if time else "",
                    "body": body.get_text("\n", strip=True) if body else ""
                })
        except Exception as e:
            print(f"[!] Failed to fetch detail page {link}: {e}")

        news_data.append({
            "title": title,
            "date": date,
            "url": link,
            "content": full_content,
            "comments": comments
        })
        updated = True

    if updated and news_data:
        save_json(news_file, news_data)
        print(f"[+] Saved {len(news_data)} news items for {course_name}")
    elif not news_data:
        print(f"[-] No news found for {course_name}")