"""
Configuration file for E3 Course Manager
"""
import os
from pathlib import Path

# Base directory for data storage
BASE_DIR = os.path.join(os.path.dirname(__file__), "test_DB")

# File paths (cross-platform compatible)
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.json")
COURSES_FILE = os.path.join(BASE_DIR, "courses_114.json")
E3_MY_HTML = os.path.join(BASE_DIR, "e3_my.html")
LAST_RUN_FILE = os.path.join(os.path.dirname(__file__), "last_run.json")

# E3 URLs
E3_BASE_URL = "https://e3p.nycu.edu.tw"
E3_LOGIN_URL = f"{E3_BASE_URL}/login/index.php"
E3_MY_URL = f"{E3_BASE_URL}/my/"
E3_NEWS_URL = f"{E3_BASE_URL}/blocks/dcpc_news/news/news_items.php"
E3_ASSIGNMENTS_URL = f"{E3_BASE_URL}/local/courseextension/index.php"
E3_GRADES_URL = f"{E3_BASE_URL}/local/courseextension/grade/report/user/index.php"

# Flask configuration
FLASK_HOST = "127.0.0.1"
FLASK_PORT = 5000
FLASK_DEBUG = False  # Set to True for development

# Selenium configuration
SELENIUM_HEADLESS = True
SELENIUM_TIMEOUT = 20

# Semester filter (can be configured)
SEMESTER_FILTER = "114下"  # Set to None to fetch all semesters

# User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# Ensure base directory exists
os.makedirs(BASE_DIR, exist_ok=True)

