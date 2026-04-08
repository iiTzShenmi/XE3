#!/usr/bin/env python3
import re
import subprocess
import sys
from pathlib import Path

from agent.core.config import cloudflared_log_file, cloudflared_url_file, port


URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def write_current_url(url):
    cloudflared_url_file().write_text(url.strip() + "\n", encoding="utf-8")


def append_log(line):
    with cloudflared_log_file().open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def main():
    command = [
        "/usr/local/bin/cloudflared",
        "tunnel",
        "--url",
        f"http://127.0.0.1:{port()}",
        "--protocol",
        "http2",
        "--no-autoupdate",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None

    for raw_line in process.stdout:
        line = raw_line.rstrip("\n")
        print(line, flush=True)
        append_log(line)
        match = URL_PATTERN.search(line)
        if match:
            write_current_url(match.group(0))

    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
