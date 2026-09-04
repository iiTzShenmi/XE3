import json
import os
import re
import shutil
import subprocess

from agent.core.config import app_service_name, cloudflared_url_file, tunnel_watchdog_state_file


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
    labels = {
        "active": "運行中",
        "inactive": "未運行",
        "failed": "失敗",
        "activating": "啟動中",
        "deactivating": "停止中",
    }
    return labels.get(state, state) or f"結束碼：{result.returncode}"


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
        return f"運行中（{url}）"
    if active:
        return "運行中（等待公開網址）"
    return "未運行"


def _watchdog_status_summary():
    active = _process_active("scripts/tunnel_watchdog.py")
    state = _read_watchdog_state() or {}
    healthy = state.get("healthy")
    detail = str(state.get("detail") or "").strip()
    if active and healthy is True:
        return "運行中（正常）"
    if active and healthy is False:
        return f"運行中（異常：{detail}）" if detail else "運行中（異常）"
    if active:
        return "運行中"
    if healthy is True:
        return "未運行（上次狀態正常）"
    if healthy is False:
        return f"未運行（上次異常：{detail}）" if detail else "未運行（上次狀態異常）"
    return "未運行"


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
        return "記憶體：無法取得"

    if not total_kb or avail_kb is None:
        return "記憶體：無法取得"

    used_kb = max(0, total_kb - avail_kb)
    used_gb = used_kb / 1024 / 1024
    total_gb = total_kb / 1024 / 1024
    percent = (used_kb / total_kb) * 100 if total_kb else 0
    return f"記憶體：{used_gb:.1f}/{total_gb:.1f} GB（{percent:.0f}%）"


def _disk_summary():
    usage = shutil.disk_usage("/")
    used_gb = (usage.total - usage.free) / 1024 / 1024 / 1024
    total_gb = usage.total / 1024 / 1024 / 1024
    percent = ((usage.total - usage.free) / usage.total) * 100 if usage.total else 0
    return f"磁碟：{used_gb:.1f}/{total_gb:.1f} GB（{percent:.0f}%）"


def _uptime_summary():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = int(float(handle.read().split()[0]))
    except OSError:
        return "運行時間：無法取得"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"運行時間：{days} 天 {hours} 小時 {minutes} 分"
    return f"運行時間：{hours} 小時 {minutes} 分"


def _load_summary():
    load1, load5, load15 = os.getloadavg()
    cores = os.cpu_count() or 1
    ratio = load1 / cores if cores else load1
    if ratio < 0.5:
        level = "輕量"
    elif ratio < 1.0:
        level = "中等"
    else:
        level = "高"
    return f"負載：{load1:.2f} / {load5:.2f} / {load15:.2f}（1/5/15 分鐘，{cores} 執行緒，{level}）"


def _cpu_summary():
    model = ""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass

    model = re.sub(r"\s+", " ", model).strip() or "無法取得型號"
    threads = os.cpu_count() or 1
    return f"CPU：{model}（{threads} 執行緒）"


def _gpu_summary():
    if not shutil.which("nvidia-smi"):
        return "GPU：未偵測到 NVIDIA GPU"

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "GPU：狀態查詢失敗"

    if result.returncode != 0:
        return "GPU：驅動或狀態查詢失敗"

    rows = [row.strip() for row in result.stdout.splitlines() if row.strip()]
    if not rows:
        return "GPU：未偵測到裝置"

    summaries = []
    for row in rows[:2]:
        parts = [part.strip() for part in row.split(",")]
        if len(parts) < 4:
            continue
        name, memory_mb, temperature, utilization = parts[:4]
        summaries.append(f"{name}｜VRAM {memory_mb} MiB｜{temperature}°C｜使用率 {utilization}%")
    return "GPU：" + "；".join(summaries) if summaries else "GPU：狀態格式無法辨識"


def build_system_report():
    service_lines = []
    for service_name in _preferred_app_services():
        state = _systemctl_state(service_name, user=True)
        if service_name == "multi-task-agent.service" and state == "未運行":
            continue
        label = {
            "discord-bot.service": "Discord Bot",
            "xe3-web.service": "Web 服務",
        }.get(service_name, "主要服務")
        service_lines.append(f"• {label}: {state}")

    return (
        "🛠️ **系統檢查**\n"
        "──────────\n"
        "📦 **服務狀態**\n"
        + "\n".join(service_lines)
        + "\n──────────\n"
        f"🌐 **Tunnel：** {_tunnel_status_summary()}\n"
        f"👀 **Watchdog：** {_watchdog_status_summary()}\n"
        "──────────\n"
        "🖥️ **硬體**\n"
        f"• {_cpu_summary()}\n"
        f"• {_gpu_summary()}\n"
        "──────────\n"
        f"🧮 **{_load_summary()}**\n"
        f"🧠 **{_memory_summary()}**\n"
        f"💾 **{_disk_summary()}**\n"
        f"⏱️ **{_uptime_summary()}**"
    )
