import difflib

from .city_data import CITY_COORDINATES
from .weather_api import get_weather
from .geolocation import get_location_from_ip, find_nearest_city, geocode_place


def handle_city_weather(city_name, logger):
    if city_name in CITY_COORDINATES:
        lat, lon = CITY_COORDINATES[city_name]
        try:
            weather = get_weather(lat, lon)
            return format_weather(city_name, weather)
        except Exception:
            logger.exception("city_weather_failed city=%s", city_name)
            return "抱歉，無法取得天氣資訊。"

    similar = [c for c in CITY_COORDINATES.keys() if city_name in c or c in city_name]
    if not similar:
        similar = difflib.get_close_matches(city_name, CITY_COORDINATES.keys(), n=5, cutoff=0.6)
    if similar:
        suggestion = "、".join(similar[:5])
        return f"找不到 '{city_name}'，你是指：{suggestion} 嗎？"

    location = geocode_place(city_name)
    if location:
        latitude, longitude = location
        nearest = find_nearest_city(latitude, longitude)
        if nearest:
            try:
                weather = get_weather(nearest["lat"], nearest["lon"])
                return format_weather(nearest["name"], weather)
            except Exception:
                logger.exception(
                    "geocoded_city_weather_failed input=%s nearest=%s",
                    city_name,
                    nearest["name"],
                )
                return "抱歉，無法取得天氣資訊。"

    return f"找不到 '{city_name}'，請查詢支援的城市或里別或嘗試再次更正名稱"


def handle_location_weather(location, logger):
    try:
        if location:
            latitude, longitude = location
            source = "GPS"
        else:
            location_data = get_location_from_ip()
            if not location_data:
                return "無法取得位置資訊（IP 定位失敗），請直接指定城市名稱"
            latitude, longitude = location_data
            source = "IP定位"

        nearest = find_nearest_city(latitude, longitude)
        weather = get_weather(nearest["lat"], nearest["lon"])
        return format_weather(nearest["name"], weather, source)
    except Exception:
        logger.exception("location_weather_failed source=%s", "gps" if location else "ip")
        return "抱歉，無法取得位置天氣資訊"


def format_weather(city, weather, source=None):
    location_info = f"📍 {city}天氣"
    if source:
        location_info += f" ({source})"

    return (
        f"🌤️ **{location_info}**\n"
        "──────────\n"
        f"🌡️ **現在溫度：** {weather['temperature']}°C\n"
        f"🥵 **體感溫度：** {weather['apparent_temperature']}°C\n"
        f"🌧️ **降雨機率：** {weather['precipitation_probability']}%\n"
        "\n"
        f"📈 **今日最高：** {weather['max_temp']}°C\n"
        f"📉 **今日最低：** {weather['min_temp']}°C"
    )
