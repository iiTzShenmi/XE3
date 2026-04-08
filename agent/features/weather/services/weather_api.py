import requests

def get_weather(lat, lon):
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}"
        f"&longitude={lon}"
        f"&current=temperature_2m,apparent_temperature,precipitation_probability"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&timezone=Asia/Taipei"
    )

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()

    current = data.get("current", {})
    daily = data.get("daily", {})

    return {
        "temperature": current.get("temperature_2m", "N/A"),
        "apparent_temperature": current.get("apparent_temperature", "N/A"),
        "precipitation_probability": current.get("precipitation_probability", "N/A"),
        "max_temp": daily.get("temperature_2m_max", ["N/A"])[0],
        "min_temp": daily.get("temperature_2m_min", ["N/A"])[0],
    }