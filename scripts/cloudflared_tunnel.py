#!/usr/bin/env python3
import re
import subprocess
import sys

from agent.core.config import cloudflared_log_file, cloudflared_url_file, port


URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
FATAL_MARKERS = ("Unauthorized: Tunnel not found",)


def write_current_url(url):
    cloudflared_url_file().write_text(url.strip() + "\n", encoding="utf-8")


def clear_current_url():
    cloudflared_url_file().unlink(missing_ok=True)


def append_log(line):
    with cloudflared_log_file().open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def stop_process(process):
    process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def main():
    clear_current_url()
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
        if any(marker in line for marker in FATAL_MARKERS):
            append_log("cloudflared fatal tunnel state detected; exiting wrapper for systemd restart")
            stop_process(process)
            return 1

    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
