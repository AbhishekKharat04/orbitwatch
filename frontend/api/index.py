from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sgp4.api import Satrec, jday


app = FastAPI(title="OrbitWatch API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"
EARTH_RADIUS_KM = 6371.0
TLE_CACHE_TTL_SECONDS = 900

_tle_cache: dict[str, dict[str, Any]] = {}
_memory_store: dict[str, Any] = {}

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
]


class LoginRequest(BaseModel):
    email: EmailStr


class VerifyRequest(BaseModel):
    email: EmailStr
    code: str


class WatchlistRequest(BaseModel):
    object_name: str
    threshold_km: float = 50.0


class AlertRequest(BaseModel):
    email: EmailStr | None = None
    threshold_km: float = 2000.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_julian_date() -> tuple[float, float]:
    now = datetime.now(timezone.utc)
    return jday(now.year, now.month, now.day, now.hour, now.minute, now.second)


def parse_tle(text: str, limit: int) -> list[tuple[str, str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tles = []
    index = 0
    while index + 2 < len(lines) and len(tles) < limit:
        name, line1, line2 = lines[index], lines[index + 1], lines[index + 2]
        if line1.startswith("1 ") and line2.startswith("2 "):
            tles.append((name[:80], line1, line2))
            index += 3
        else:
            index += 1
    return tles


def fetch_celestrak_tles(group: str = "STATIONS", limit: int = 80) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    group = group.upper()
    cache_key = f"{group}:{limit}"
    cached = _tle_cache.get(cache_key)
    if cached and time.time() - cached["fetched_at"] < TLE_CACHE_TTL_SECONDS:
        return cached["tles"], cached["meta"]

    try:
        response = requests.get(
            CELESTRAK_URL,
            params={"GROUP": group, "FORMAT": "TLE"},
            timeout=8,
            headers={"User-Agent": "OrbitWatch/2.0"},
        )
        response.raise_for_status()
        tles = parse_tle(response.text, limit)
        if not tles:
            raise ValueError("CelesTrak returned no TLE rows")

        meta = {
            "source": "CelesTrak GP",
            "group": group,
            "catalog_mode": "real_time_tle",
            "fetched_at": now_iso(),
            "cache_ttl_seconds": TLE_CACHE_TTL_SECONDS,
            "fallback": False,
        }
    except Exception as exc:
        tles = DEMO_TLES
        meta = {
            "source": "fallback demo TLE catalog",
            "group": "DEMO",
            "catalog_mode": "fallback_demo",
            "fetched_at": now_iso(),
            "cache_ttl_seconds": TLE_CACHE_TTL_SECONDS,
            "fallback": True,
            "error": str(exc),
        }

    _tle_cache[cache_key] = {"tles": tles, "meta": meta, "fetched_at": time.time()}
    return tles, meta


def get_position(name: str, line1: str, line2: str) -> dict[str, Any] | None:
    sat = Satrec.twoline2rv(line1, line2)
    error, position, velocity = sat.sgp4(*current_julian_date())
    if error != 0:
        return None

    radius = math.sqrt(position[0] ** 2 + position[1] ** 2 + position[2] ** 2)
    speed = math.sqrt(velocity[0] ** 2 + velocity[1] ** 2 + velocity[2] ** 2)
    return {
        "name": name,
        "x": round(position[0], 3),
        "y": round(position[1], 3),
        "z": round(position[2], 3),
        "vx": round(velocity[0], 6),
        "vy": round(velocity[1], 6),
        "vz": round(velocity[2], 6),
        "radius_km": round(radius, 3),
        "alt_km": round(radius - EARTH_RADIUS_KM, 1),
        "speed_km_s": round(speed, 4),
        "frame": "TEME",
    }


def satellite_positions(group: str = "STATIONS", limit: int = 80) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tles, meta = fetch_celestrak_tles(group, limit)
    positions = [position for tle in tles if (position := get_position(*tle))]
    return positions, meta


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
        "HIGH": f"HIGH: {first} and {second} are {distance:.2f} km apart. Suggested 0.5-2 m/s retrograde burn assessment.",
        "MEDIUM": f"MEDIUM: {first} and {second} are within {distance:.2f} km. Continue tracking and prepare optional maneuver.",
        "LOW": f"LOW: {first} and {second} are {distance:.2f} km apart. Monitor as nearest-current-approach context.",
    }
    return messages[risk]


def detect_conjunctions(positions: list[dict[str, Any]], threshold_km: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pairs = []
    for index, first in enumerate(positions):
        for second in positions[index + 1 :]:
            distance = distance_km(first, second)
            risk = risk_level(distance)
            event = {
                "sat1": first["name"],
                "sat2": second["name"],
                "distance_km": round(distance, 3),
                "risk_level": risk,
                "alert": alert_message(first["name"], second["name"], distance, risk),
                "sat1_alt": first["alt_km"],
                "sat2_alt": second["alt_km"],
                "timestamp": now_iso(),
            }
            pairs.append(event)

    pairs.sort(key=lambda event: event["distance_km"])
    events = [event for event in pairs if event["distance_km"] <= threshold_km]
    return events, pairs[:20]


def integration_status() -> dict[str, bool]:
    return {
        "celestrak": True,
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "database": bool(os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN")),
        "email_alerts": bool(os.getenv("RESEND_API_KEY")),
    }


def sign_session(email: str) -> str:
    secret = os.getenv("APP_SECRET", "orbitwatch-dev-secret")
    payload = {"email": email, "exp": int(time.time()) + 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_session(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        encoded, signature = token.split(".", 1)
        secret = os.getenv("APP_SECRET", "orbitwatch-dev-secret")
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "==="))
        if payload["exp"] < time.time():
            raise ValueError("expired")
        return payload["email"]
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc


def store_set(key: str, value: Any) -> None:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=["SET", key, json.dumps(value)],
            timeout=6,
        ).raise_for_status()
    else:
        _memory_store[key] = value


def store_get(key: str, default: Any = None) -> Any:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=["GET", key],
            timeout=6,
        )
        response.raise_for_status()
        result = response.json().get("result")
        return json.loads(result) if result else default
    return _memory_store.get(key, default)


def send_email(to_email: str, subject: str, html: str) -> dict[str, Any]:
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        return {"sent": False, "reason": "RESEND_API_KEY is not configured"}

    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": os.getenv("ALERT_FROM_EMAIL", "OrbitWatch <onboarding@resend.dev>"),
            "to": [to_email],
            "subject": subject,
            "html": html,
        },
        timeout=10,
    )
    response.raise_for_status()
    return {"sent": True, "provider_response": response.json()}


def llm_briefing(events: list[dict[str, Any]], stats_payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {
            "mode": "rules_fallback",
            "model": None,
            "briefing": "OPENAI_API_KEY is not configured. OrbitWatch is using deterministic risk rules for this briefing.",
        }

    prompt = {
        "role": "user",
        "content": (
            "You are OrbitWatch, a satellite collision-risk operations assistant. "
            "Write a concise operator briefing with risk level, top event, and recommended next action. "
            f"Stats: {json.dumps(stats_payload)} Events: {json.dumps(events[:5])}"
        ),
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            "input": [prompt],
            "max_output_tokens": 220,
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    text = data.get("output_text")
    if not text:
        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))
        text = " ".join(chunks).strip()
    return {"mode": "openai_responses_api", "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "briefing": text}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "OrbitWatch API",
        "status": "operational",
        "version": "2.0.0",
        "integrations": integration_status(),
    }


@app.get("/api/stats")
def stats(
    group: str = Query(default="STATIONS"),
    limit: int = Query(default=80, ge=4, le=250),
) -> dict[str, Any]:
    positions, meta = satellite_positions(group, limit)
    return {
        "tracked_objects": len(positions),
        "active_satellites": len([sat for sat in positions if "DEB" not in sat["name"].upper()]),
        "debris_fragments": len([sat for sat in positions if "DEB" in sat["name"].upper()]),
        "high_risk_events_today": 0,
        "maneuvers_recommended": 0,
        "last_updated": now_iso(),
        "data_source": meta["source"],
        "runtime": "Vercel Python Serverless",
        "catalog": meta,
        "coordinate_frame": "TEME",
        "integrations": integration_status(),
    }


@app.get("/api/satellites")
def satellites(
    group: str = Query(default="STATIONS"),
    limit: int = Query(default=80, ge=4, le=250),
) -> dict[str, Any]:
    positions, meta = satellite_positions(group, limit)
    return {
        "count": len(positions),
        "satellites": positions,
        "catalog": meta,
        "coordinate_frame": "TEME",
        "timestamp": now_iso(),
    }


@app.get("/api/conjunctions")
def conjunctions(
    threshold_km: float = Query(default=2000.0, ge=0.1, le=20000.0),
    group: str = Query(default="STATIONS"),
    limit: int = Query(default=80, ge=4, le=250),
) -> dict[str, Any]:
    positions, meta = satellite_positions(group, limit)
    events, nearest = detect_conjunctions(positions, threshold_km)
    return {
        "total": len(events),
        "threshold_km": threshold_km,
        "conjunctions": events[:20],
        "nearest_approaches": nearest,
        "catalog": meta,
        "coordinate_frame": "TEME",
        "timestamp": now_iso(),
    }


@app.get("/api/agent-briefing")
def agent_briefing(
    threshold_km: float = Query(default=2000.0, ge=0.1, le=20000.0),
    group: str = Query(default="STATIONS"),
    limit: int = Query(default=80, ge=4, le=250),
) -> dict[str, Any]:
    positions, meta = satellite_positions(group, limit)
    events, nearest = detect_conjunctions(positions, threshold_km)
    stats_payload = {"tracked_objects": len(positions), "catalog": meta, "threshold_km": threshold_km}
    return {
        **llm_briefing(events or nearest[:5], stats_payload),
        "events_used": events[:5] or nearest[:5],
        "integrations": integration_status(),
        "timestamp": now_iso(),
    }


@app.post("/api/auth/request-code")
def request_code(payload: LoginRequest) -> dict[str, Any]:
    code = f"{secrets.randbelow(1000000):06d}"
    store_set(f"login:{payload.email}", {"code": code, "expires_at": time.time() + 600})
    result = send_email(
        payload.email,
        "OrbitWatch login code",
        f"<p>Your OrbitWatch login code is <strong>{code}</strong>. It expires in 10 minutes.</p>",
    )
    return {
        "ok": True,
        "email": payload.email,
        "email_delivery": result,
        "dev_code": None if result.get("sent") else code,
    }


@app.post("/api/auth/verify-code")
def verify_code(payload: VerifyRequest) -> dict[str, Any]:
    record = store_get(f"login:{payload.email}")
    if not record or record.get("expires_at", 0) < time.time() or record.get("code") != payload.code:
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    store_set(f"user:{payload.email}", {"email": payload.email, "created_at": now_iso()})
    return {"ok": True, "token": sign_session(payload.email), "email": payload.email}


@app.get("/api/user/watchlist")
def get_watchlist(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session(authorization)
    return {"email": email, "watchlist": store_get(f"watchlist:{email}", [])}


@app.post("/api/user/watchlist")
def add_watchlist(payload: WatchlistRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session(authorization)
    items = store_get(f"watchlist:{email}", [])
    item = {"object_name": payload.object_name, "threshold_km": payload.threshold_km, "created_at": now_iso()}
    items.append(item)
    store_set(f"watchlist:{email}", items)
    return {"email": email, "watchlist": items}


@app.post("/api/alerts/test")
def send_test_alert(payload: AlertRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = payload.email or verify_session(authorization)
    positions, _meta = satellite_positions("STATIONS", 80)
    events, nearest = detect_conjunctions(positions, payload.threshold_km)
    top = (events or nearest)[0] if (events or nearest) else None
    if not top:
        raise HTTPException(status_code=404, detail="No tracked approaches available")
    result = send_email(
        email,
        f"OrbitWatch {top['risk_level']} alert",
        f"<p>{top['alert']}</p><p>Distance: {top['distance_km']} km</p>",
    )
    return {"ok": True, "email": email, "top_event": top, "delivery": result}
