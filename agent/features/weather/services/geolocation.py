import requests


def get_location_from_ip(user_id=None):
    """
    Get approximate coordinates from user's IP address.

    Args:
        user_id: reserved for future per-user location tracking

    Returns:
        tuple: (latitude, longitude) or None if failed
    """
    try:
        response = requests.get("https://ip-api.com/json/?fields=lat,lon,status", timeout=3)
        data = response.json()
        if data.get("status") == "success":
            return (data.get("lat"), data.get("lon"))
    except Exception as exc:
        print(f"IP geolocation failed: {exc}")
        return None


def find_nearest_city(latitude, longitude):
    """Find the nearest supported city/district to given coordinates."""
    from ..data.city_data import CITY_COORDINATES
    import math

    def distance(lat1, lon1, lat2, lon2):
        r_km = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        c = 2 * math.asin(math.sqrt(a))
        return r_km * c

    nearest = None
    min_distance = float("inf")

    for city_name, (city_lat, city_lon) in CITY_COORDINATES.items():
        dist = distance(latitude, longitude, city_lat, city_lon)
        if dist < min_distance:
            min_distance = dist
            nearest = {
                "name": city_name,
                "lat": city_lat,
                "lon": city_lon,
                "distance_km": round(dist, 2),
            }

    return nearest


def geocode_place(name):
    """Try to geocode a place name using OpenStreetMap Nominatim."""
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": name,
            "countrycodes": "tw",
            "format": "json",
            "limit": 1,
        }
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": "multi-task-agent/1.0"},
            timeout=5,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return (float(results[0]["lat"]), float(results[0]["lon"]))
    except Exception as exc:
        print(f"Geocoding failed for '{name}': {exc}")
    return None
