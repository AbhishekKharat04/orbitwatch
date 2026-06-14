from datetime import datetime, timezone
import math
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sgp4.api import Satrec, jday


app = FastAPI(
    title="OrbitWatch API",
    version="1.0.0",
    description="Agentic satellite conjunction monitoring demo for FAR AWAY 2026.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEMO_TLES = [
    (
        "ISS (ZARYA)",
        "1 25544U 98067A   24001.50000000  .00005000  00000-0  90000-4 0  9999",
        "2 25544  51.6400 200.0000 0002000  90.0000 270.1000 15.50000000000000",
    ),
    (
        "STARLINK-1234",
        "1 44235U 19029D   24001.50000000  .00000100  00000-0  10000-4 0  9999",
        "2 44235  53.0000 100.0000 0001000  45.0000 315.0000 15.06400000000000",
    ),
    (
        "DEBRIS-A",
        "1 12345U 85001A   24001.50020000  .00000010  00000-0  12000-4 0  9999",
        "2 12345  51.5000 201.0000 0002100  89.5000 271.0000 15.49800000000000",
    ),
    (
        "COSMOS 2251 DEB",
        "1 34341U 93036PX  24001.50100000  .00000050  00000-0  50000-4 0  9999",
        "2 34341  74.0500 150.0000 0010000  60.0000 300.0000 14.80000000000000",
    ),
    (
        "FENGYUN 1C DEB",
        "1 29228U 99025CKC 24001.50200000  .00000030  00000-0  30000-4 0  9999",
        "2 29228  98.8200 220.0000 0008000  30.0000 330.0000 14.30000000000000",
    ),
    (
        "IRIDIUM 33 DEB",
        "1 33442U 97051CE  24001.50300000  .00000020  00000-0  25000-4 0  9999",
        "2 33442  86.4000 180.0000 0003000  70.0000 290.0000 14.90000000000000",
    ),
    (
        "SL-16 R/B",
        "1 19650U 88109B   24001.50400000  .00000005  00000-0  80000-5 0  9999",
        "2 19650  71.0000 250.0000 0005000  80.0000 280.0000 14.00000000000000",
    ),
    (
        "METEOR 2-5 DEB",
        "1 10049U 77099B   24001.50500000  .00000015  00000-0  15000-4 0  9999",
        "2 10049  81.2000 320.0000 0012000  40.0000 320.0000 13.50000000000000",
    ),
]


def current_julian_date() -> tuple[float, float]:
    now = datetime.now(timezone.utc)
    return jday(now.year, now.month, now.day, now.hour, now.minute, now.second)


def get_position(name: str, line1: str, line2: str) -> dict[str, Any] | None:
    sat = Satrec.twoline2rv(line1, line2)
    error, position, _velocity = sat.sgp4(*current_julian_date())
    if error != 0:
        return None

    altitude = math.sqrt(position[0] ** 2 + position[1] ** 2 + position[2] ** 2) - 6371.0
    return {
        "name": name,
        "x": round(position[0], 3),
        "y": round(position[1], 3),
        "z": round(position[2], 3),
        "alt_km": round(altitude, 1),
    }


def distance_km(first: dict[str, Any], second: dict[str, Any]) -> float:
    return math.sqrt(
        (first["x"] - second["x"]) ** 2
        + (first["y"] - second["y"]) ** 2
        + (first["z"] - second["z"]) ** 2
    )


def risk_level(distance: float) -> str:
    if distance < 1:
        return "CRITICAL"
    if distance < 5:
        return "HIGH"
    if distance < 20:
        return "MEDIUM"
    return "LOW"


def alert_message(first: str, second: str, distance: float, risk: str) -> str:
    messages = {
        "CRITICAL": f"CRITICAL: {first} and {second} are {distance:.2f} km apart. Immediate maneuver review required.",
        "HIGH": f"HIGH: {first} and {second} are {distance:.2f} km apart. Suggested 0.5-2 m/s retrograde burn window.",
        "MEDIUM": f"MEDIUM: {first} and {second} are within {distance:.2f} km. Continue tracking and prepare optional maneuver.",
        "LOW": f"LOW: {first} and {second} are {distance:.2f} km apart. No action required.",
    }
    return messages[risk]


def satellite_positions() -> list[dict[str, Any]]:
    return [position for tle in DEMO_TLES if (position := get_position(*tle))]


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "OrbitWatch",
        "status": "operational",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.get("/api/satellites")
def satellites() -> dict[str, Any]:
    positions = satellite_positions()
    return {
        "count": len(positions),
        "satellites": positions,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/conjunctions")
def conjunctions(
    threshold_km: float = Query(default=50.0, ge=0.1, le=10000.0),
) -> dict[str, Any]:
    positions = satellite_positions()
    events = []

    for index, first in enumerate(positions):
        for second in positions[index + 1 :]:
            distance = distance_km(first, second)
            if distance <= threshold_km:
                risk = risk_level(distance)
                events.append(
                    {
                        "sat1": first["name"],
                        "sat2": second["name"],
                        "distance_km": round(distance, 3),
                        "risk_level": risk,
                        "alert": alert_message(first["name"], second["name"], distance, risk),
                        "sat1_alt": first["alt_km"],
                        "sat2_alt": second["alt_km"],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    events.sort(key=lambda event: event["distance_km"])
    return {
        "total": len(events),
        "threshold_km": threshold_km,
        "conjunctions": events[:20],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return {
        "tracked_objects": 27686,
        "active_satellites": 8963,
        "debris_fragments": 18723,
        "high_risk_events_today": 4,
        "maneuvers_recommended": 1,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "data_source": "CelesTrak / NORAD demo catalog",
    }
