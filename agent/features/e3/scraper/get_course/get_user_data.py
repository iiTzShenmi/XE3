import os
import json
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from .extract_course import extract_course
from .. import config

COOKIE_FILE = config.COOKIE_FILE


def _clear_cookie_file():
    try:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
    except OSError:
        pass

def login_and_get_cookies(account, password):
    chrome_options = Options()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--window-size=1440,1024")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--remote-debugging-port=9222")
    if config.SELENIUM_HEADLESS:
        chrome_options.add_argument("--headless=new")
    else:
        chrome_options.add_argument("--start-maximized")
    _clear_cookie_file()
    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(config.SELENIUM_PAGE_LOAD_TIMEOUT)

    try:
        print("[*] Opening E3 login page...")
        driver.get(config.E3_LOGIN_URL)

        WebDriverWait(driver, config.SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "username"))
        )

        driver.find_element(By.ID, "username").send_keys(account)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.ID, "loginbtn").click()

        # Wait until redirected to /my/ dashboard
        WebDriverWait(driver, config.SELENIUM_TIMEOUT).until(
            EC.url_contains("/my/")
        )

        print("[+] Login success!")

        cookies = driver.get_cookies()
        requests_cookies = {c["name"]: c["value"] for c in cookies}
        os.makedirs(config.BASE_DIR, exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(requests_cookies, f, ensure_ascii=False, indent=2)

        print("[+] Cookies saved")
        return requests_cookies

    except Exception as e:
        print(f"[!] Login failed: {e}")
        return {}
    finally:
        driver.quit()


def load_cookies():
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
            if isinstance(cookies, dict):
                return cookies
            else:
                print("[!] Invalid cookies.json format, re-login...")
                return {}
    except FileNotFoundError:
        return {}


def _save_cookies(cookie_dict):
    os.makedirs(config.BASE_DIR, exist_ok=True)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookie_dict, f, ensure_ascii=False, indent=2)


def _cookie_dict_from_session(session):
    return requests.utils.dict_from_cookiejar(session.cookies)


def build_authenticated_session(cookies=None):
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.USER_AGENT,
        "Referer": config.E3_BASE_URL + "/",
    })
    if cookies:
        session.cookies.update(cookies)
    original_request = session.request

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", config.REQUEST_TIMEOUT)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    return session


def _needs_relogin(resp):
    if not resp:
        return True
    text = resp.text or ""
    url = resp.url or ""
    return "login" in url or "登入本網站" in text


def fetch_e3_my(account, password):
    """Fetch dashboard HTML and return the HTML text."""
    url = config.E3_MY_URL
    cookies = load_cookies()
    if not cookies:
        cookies = login_and_get_cookies(account, password)

    session = build_authenticated_session(cookies)

    print("[*] Fetching dashboard page...")
    try:
        resp = session.get(url, allow_redirects=True, timeout=15.0)
    except requests.TooManyRedirects:
        print("[!] Too many redirects detected, refreshing cookies...")
        _clear_cookie_file()
        cookies = login_and_get_cookies(account, password)
        session = build_authenticated_session(cookies)
        resp = session.get(url, allow_redirects=True, timeout=15.0)

    # Detect redirect loops / login page
    if resp.history and len(resp.history) > 5:
        print("[!] Too many redirects detected, refreshing cookies...")
        _clear_cookie_file()
        cookies = login_and_get_cookies(account, password)
        session = build_authenticated_session(cookies)
        resp = session.get(url, allow_redirects=True, timeout=15.0)

    if _needs_relogin(resp):
        print("[!] Cookies invalid, re-login...")
        _clear_cookie_file()
        cookies = login_and_get_cookies(account, password)
        session = build_authenticated_session(cookies)
        resp = session.get(url, allow_redirects=True, timeout=15.0)

    if resp.ok:
        print("[+] Success, got dashboard content!")
        os.makedirs(config.BASE_DIR, exist_ok=True)
        with open(config.E3_MY_HTML, "w", encoding="utf-8") as f:
            f.write(resp.text)
        _save_cookies(_cookie_dict_from_session(session))
        return resp.text
    else:
        print(f"[!] Failed to fetch dashboard: {resp.status_code}")
        return None


def ensure_authenticated_session(account, password):
    """Return a requests.Session authenticated for E3, using Selenium only if cookies must be refreshed."""
    html = fetch_e3_my(account, password)
    if not html:
        return None, None
    cookies = load_cookies()
    session = build_authenticated_session(cookies)
    return session, cookies


def get_user_data(account, password, update_data=True, update_links=False):
    """
    Fetch dashboard page and extract course list.
    Returns: dict {course_id: course_name} or {}
    
    Args:
        update_data: If True, update course data (news, assignments, grades)
        update_links: If True, update file links database
    """
    session, cookies = ensure_authenticated_session(account, password)
    if session:
        courses = extract_course()
        if update_data:
            try:
                from ..update_all import __update_course_data
                __update_course_data(session=session, cookies=cookies)
            except Exception as e:
                print(f"[!] Warning: Could not update course data: {e}")

        if update_links:
            try:
                from ..update_all import __update_file_links
                __update_file_links(session=session, cookies=cookies)
            except Exception as e:
                print(f"[!] Warning: Could not update file links: {e}")

        return courses
    return {}
