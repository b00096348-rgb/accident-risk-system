"""Module for querying spatial road context from OpenStreetMap."""

import math
import time
import requests


class MapContextResolver:
    """Queries OpenStreetMap to check if GPS coordinates match a roundabout."""

    def __init__(self, cache_ttl_sec: float = 10.0, radius_meters: float = 30.0):
        self.cache_ttl_sec = cache_ttl_sec
        self.radius_meters = radius_meters
        self._last_lat = None
        self._last_lon = None
        self._last_query_time = 0.0
        self._last_result = False

    def _haversine_meters(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great-circle distance between two points in meters."""
        r = 6371000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def is_in_roundabout(self, lat: float | None, lon: float | None) -> bool:
        """Check if latitude/longitude falls on or near a roundabout."""
        if lat is None or lon is None or lat == 0.0 or lon == 0.0:
            return False

        now = time.time()
        if self._last_lat is not None and self._last_lon is not None:
            dist = self._haversine_meters(self._last_lat, self._last_lon, lat, lon)
            if dist < 15.0 and (now - self._last_query_time) < self.cache_ttl_sec:
                return self._last_result

        query = f"""
        [out:json][timeout:3];
        (
          way(around:{self.radius_meters},{lat},{lon})["junction"="roundabout"];
          way(around:{self.radius_meters},{lat},{lon})["junction"="circular"];
          node(around:{self.radius_meters},{lat},{lon})["highway"="mini_roundabout"];
        );
        out body;
        """
        try:
            response = requests.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
                timeout=2.0,
            )
            if response.status_code == 200:
                elements = response.json().get("elements", [])
                self._last_result = len(elements) > 0
                self._last_lat = lat
                self._last_lon = lon
                self._last_query_time = now
                return self._last_result
        # pylint: disable=broad-exception-caught
        except Exception:
            pass

        return self._last_result
    