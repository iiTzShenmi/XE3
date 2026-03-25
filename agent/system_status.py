import json
import os
import shutil
import subprocess

from agent.config import app_service_name, cloudflared_url_file, tunnel_watchdog_state_file


def _systemctl_state(unit_name, user=False):
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(["is-active", unit_name])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except Exception as exc:  # pragma: no cover
        return f"error:{exc}"
    state = (result.stdout or result.stderr or "").strip()
    return state or f"exit:{result.returncode}"


def _preferred_app_services():
    configured = app_service_name()
    services = []
    for candidate in [configured, "discord-bot.service", "xe3-web.service"]:
        name = str(candidate or "").strip()
        if name and name not in services:
            services.append(name)
    return services


def _process_active(pattern):
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # pragma: no cover
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _read_watchdog_state():
    path = tunnel_watchdog_state_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tunnel_status_summary():
    url_path = cloudflared_url_file()
    url = ""
    if url_path.exists():
        try:
            url = url_path.read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
    active = _process_active("cloudflared tunnel --url")
    if active and url:
        return f"active ({url})"
    if active:
        return "active (waiting for public url)"
    return "inactive"


def _watchdog_status_summary():
    active = _process_active("scripts/tunnel_watchdog.py")
    state = _read_watchdog_state() or {}
    healthy = state.get("healthy")
    detail = str(state.get("detail") or "").strip()
    if active and healthy is True:
        return "active (healthy)"
    if active and healthy is False:
        return f"active (unhealthy: {detail})" if detail else "active (unhealthy)"
    if active:
        return "active"
    if healthy is True:
        return "inactive (last seen healthy)"
    if healthy is False:
        return f"inactive (last seen unhealthy: {detail})" if detail else "inactive (last seen unhealthy)"
    return "inactive"


def _memory_summary():
    total_kb = None
    avail_kb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
    except OSError:
        return "Memory: unknown"

    if not total_kb or avail_kb is None:
        return "Memory: unknown"

    used_kb = max(0, total_kb - avail_kb)
    used_gb = used_kb / 1024 / 1024
    total_gb = total_kb / 1024 / 1024
    percent = (used_kb / total_kb) * 100 if total_kb else 0
    return f"Memory: {used_gb:.1f}/{total_gb:.1f} GB ({percent:.0f}%)"


def _disk_summary():
    usage = shutil.disk_usage("/")
    used_gb = (usage.total - usage.free) / 1024 / 1024 / 1024
    total_gb = usage.total / 1024 / 1024 / 1024
    percent = ((usage.total - usage.free) / usage.total) * 100 if usage.total else 0
    return f"Disk: {used_gb:.1f}/{total_gb:.1f} GB ({percent:.0f}%)"


def _uptime_summary():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = int(float(handle.read().split()[0]))
    except OSError:
        return "Uptime: unknown"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"Uptime: {days}d {hours}h {minutes}m"
    return f"Uptime: {hours}h {minutes}m"


def _load_summary():
    load1, load5, load15 = os.getloadavg()
    cores = os.cpu_count() or 1
    ratio = load1 / cores if cores else load1
    if ratio < 0.5:
        level = "light"
    elif ratio < 1.0:
        level = "moderate"
    else:
        level = "high"
    return f"Load: {load1:.2f} / {load5:.2f} / {load15:.2f} (1/5/15 min, {cores} cores, {level})"


def build_system_report():
    service_lines = []
    for service_name in _preferred_app_services():
        state = _systemctl_state(service_name, user=True)
        if service_name == "multi-task-agent.service" and state == "inactive":
            continue
        label = {
            "discord-bot.service": "Discord bot",
            "xe3-web.service": "Web service",
        }.get(service_name, "Main service")
        service_lines.append(f"• {label}: {state}")

    return (
        "🛠️ **系統檢查**\n"
        "──────────\n"
        "📦 **Services**\n"
        + "\n".join(service_lines)
        + "\n──────────\n"
        f"🌐 **Tunnel:** {_tunnel_status_summary()}\n"
        f"👀 **Watchdog:** {_watchdog_status_summary()}\n"
        "──────────\n"
        f"🧮 **{_load_summary()}**\n"
        f"🧠 **{_memory_summary()}**\n"
        f"💾 **{_disk_summary()}**\n"
        f"⏱️ **{_uptime_summary()}**"
    )
