from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()
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
    requested_role: str | None = None


class RoleChangeRequest(BaseModel):
    target_email: EmailStr
    new_role: str



class WatchlistRequest(BaseModel):
    object_name: str
    threshold_km: float = 50.0


class AlertRequest(BaseModel):
    email: EmailStr | None = None
    threshold_km: float = 2000.0


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    group: str = "STATIONS"
    limit: int = 80
    threshold_km: float = 2000.0


class ManeuverRequest(BaseModel):
    sat1_name: str
    sat2_name: str
    burn_satellite: str
    burn_direction: str
    delta_v_m_s: float
    burn_hours_before_tca: float
    group: str = "STATIONS"
    limit: int = 80


class ManeuverSolveRequest(BaseModel):
    sat1_name: str
    sat2_name: str
    group: str = "STATIONS"
    limit: int = 80



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


def map_celestrak_to_dict(name: str, line1: str, line2: str) -> dict[str, Any]:
    name_upper = name.upper()
    obj_type = "PAYLOAD"
    if "DEB" in name_upper:
        obj_type = "DEBRIS"
    elif "R/B" in name_upper or "ROCKET" in name_upper:
        obj_type = "ROCKET BODY"
        
    country = "UNKNOWN"
    if "STARLINK" in name_upper or "GPS" in name_upper:
        country = "USA"
    elif "ISS" in name_upper:
        country = "INT"
    elif "TIANGONG" in name_upper or "CSS" in name_upper:
        country = "PRC"
    elif "COSMOS" in name_upper:
        country = "RUS"
        
    norad_id = line1[2:7].strip()
    return {
        "name": name,
        "tle1": line1,
        "tle2": line2,
        "country": country,
        "object_type": obj_type,
        "norad_id": norad_id
    }


def fetch_spacetrack_gp(group: str, limit: int) -> list[dict[str, Any]]:
    user = os.getenv("SPACETRACK_USER")
    password = os.getenv("SPACETRACK_PASSWORD")
    if not user or not password:
        raise ValueError("Space-Track credentials not set")
        
    group = group.upper()
    if group == "STATIONS":
        filter_str = "OBJECT_NAME/ISS~~TIANGONG~~ZARYA~~KIBO~~COLUMBUS~~DESTINY~~POISK~~ZVEZDA/"
    elif group == "STARLINK":
        filter_str = "OBJECT_NAME/STARLINK/"
    elif group == "GPS-OPS":
        filter_str = "OBJECT_NAME/GPS/"
    elif group == "WEATHER":
        filter_str = "OBJECT_NAME/NOAA~~METEOR~~FENGYUN~~GOES/"
    else:
        filter_str = ""
        
    login_url = "https://www.space-track.org/ajaxauth/login"
    query_url = f"https://www.space-track.org/basicspacetrack/query/class/gp/{filter_str}limit/{limit}/orderby/EPOCH%20desc/format/json"
    
    session = requests.Session()
    resp = session.post(login_url, data={"identity": user, "password": password}, timeout=10)
    resp.raise_for_status()
    if "Failed" in resp.text or "Incorrect" in resp.text:
        raise ValueError("Space-Track login failed: invalid credentials")
        
    query_resp = session.get(query_url, timeout=15)
    query_resp.raise_for_status()
    raw_data = query_resp.json()
    
    results = []
    for item in raw_data:
        name = item.get("OBJECT_NAME", "UNKNOWN")
        tle1 = item.get("TLE_LINE1")
        tle2 = item.get("TLE_LINE2")
        if not tle1:
            tle1 = item.get("LINE1")
        if not tle2:
            tle2 = item.get("LINE2")
            
        if not tle1 or not tle2:
            continue
            
        results.append({
            "name": name,
            "tle1": tle1,
            "tle2": tle2,
            "country": item.get("COUNTRY", "UNKNOWN") or "UNKNOWN",
            "object_type": item.get("OBJECT_TYPE", "PAYLOAD") or "PAYLOAD",
            "norad_id": item.get("NORAD_CAT_ID", tle1[2:7].strip())
        })
    return results


def fetch_telemetry_catalog(group: str = "STATIONS", limit: int = 80) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    group = group.upper()
    cache_key = f"catalog:{group}:{limit}"
    cached = _tle_cache.get(cache_key)
    if cached and time.time() - cached["fetched_at"] < TLE_CACHE_TTL_SECONDS:
        return cached["catalog"], cached["meta"]
        
    catalog = []
    source = ""
    fallback = False
    error_msg = None
    
    user = os.getenv("SPACETRACK_USER")
    password = os.getenv("SPACETRACK_PASSWORD")
    if user and password:
        try:
            catalog = fetch_spacetrack_gp(group, limit)
            source = "Space-Track basicspacetrack GP API"
        except Exception as exc:
            error_msg = str(exc)
            
    if not catalog:
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
            
            catalog = [map_celestrak_to_dict(name, line1, line2) for name, line1, line2 in tles]
            source = "CelesTrak GP (Space-Track fallback)" if error_msg else "CelesTrak GP"
            if error_msg:
                source += f" [Error: {error_msg}]"
        except Exception as exc:
            catalog = [map_celestrak_to_dict(name, line1, line2) for name, line1, line2 in DEMO_TLES]
            source = "fallback demo TLE catalog"
            fallback = True
            error_msg = str(exc)
            
    meta = {
        "source": source,
        "group": group,
        "catalog_mode": "spacetrack" if "Space-Track" in source else ("celestrak" if not fallback else "fallback_demo"),
        "fetched_at": now_iso(),
        "cache_ttl_seconds": TLE_CACHE_TTL_SECONDS,
        "fallback": fallback,
    }
    if error_msg:
        meta["error"] = error_msg
        
    _tle_cache[cache_key] = {"catalog": catalog, "meta": meta, "fetched_at": time.time()}
    return catalog, meta


def fetch_celestrak_tles(group: str = "STATIONS", limit: int = 80) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    catalog, meta = fetch_telemetry_catalog(group, limit)
    tles = [(item["name"], item["tle1"], item["tle2"]) for item in catalog]
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
    catalog, meta = fetch_telemetry_catalog(group, limit)
    positions = []
    for item in catalog:
        try:
            sat = Satrec.twoline2rv(item["tle1"], item["tle2"])
            error, position, velocity = sat.sgp4(*current_julian_date())
            if error == 0:
                radius = math.sqrt(position[0] ** 2 + position[1] ** 2 + position[2] ** 2)
                speed = math.sqrt(velocity[0] ** 2 + velocity[1] ** 2 + velocity[2] ** 2)
                positions.append({
                    "name": item["name"],
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
                    "country": item["country"],
                    "object_type": item["object_type"],
                    "norad_id": item["norad_id"]
                })
        except Exception:
            continue
    return positions, meta


def risk_level(distance: float) -> str:
    if distance < 5.0:
        return "CRITICAL"
    if distance < 25.0:
        return "HIGH"
    if distance < 100.0:
        return "MEDIUM"
    return "LOW"


def alert_message(first: str, second: str, distance: float, risk: str) -> str:
    messages = {
        "CRITICAL": f"CRITICAL: {first} and {second} are projected to pass within {distance:.2f} km. Immediate maneuver review required.",
        "HIGH": f"HIGH: {first} and {second} are projected to pass within {distance:.2f} km. Suggested maneuver assessment.",
        "MEDIUM": f"MEDIUM: {first} and {second} will approach within {distance:.2f} km. Continue monitoring.",
        "LOW": f"LOW: {first} and {second} will pass within {distance:.2f} km. Monitor passively.",
    }
    return messages[risk]


def detect_time_series_conjunctions(catalog: list[dict[str, Any]], threshold_km: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sat_data = []
    for item in catalog:
        if item.get("is_custom"):
            sat_data.append((item["name"], "custom", item))
        else:
            try:
                sat = Satrec.twoline2rv(item["tle1"], item["tle2"])
                sat_data.append((item["name"], sat, item))
            except Exception:
                continue

    num_sats = len(sat_data)
    if num_sats < 2:
        return [], []

    now = datetime.now(timezone.utc)

    # Coarse search: 16 steps over 24 hours (every 1.5 hours)
    coarse_steps = []
    for i in range(16):
        dt = now + timedelta(hours=i * 1.5)
        jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        coarse_steps.append((jd, fr, dt))

    # Pre-propagate positions at coarse steps
    coarse_positions = []
    for name, sat, item in sat_data:
        sat_positions = []
        if sat == "custom":
            launch_time = datetime.fromisoformat(item["launch_time"])
            r0 = [item["init_x"], item["init_y"], item["init_z"]]
            v0 = [item["init_vx"], item["init_vy"], item["init_vz"]]
            for _, _, step_dt in coarse_steps:
                dt_sec = (step_dt - launch_time).total_seconds()
                pos_t, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
                sat_positions.append(pos_t)
        else:
            for jd, fr, _ in coarse_steps:
                err, pos, _ = sat.sgp4(jd, fr)
                if err == 0:
                    sat_positions.append(pos)
                else:
                    sat_positions.append(None)
        coarse_positions.append(sat_positions)

    conjunction_candidates = []

    for i in range(num_sats):
        for j in range(i + 1, num_sats):
            min_dist_sq = float("inf")
            min_step_idx = -1
            for step_idx in range(16):
                pos1 = coarse_positions[i][step_idx]
                pos2 = coarse_positions[j][step_idx]
                if pos1 is not None and pos2 is not None:
                    d_sq = (pos1[0] - pos2[0]) ** 2 + (pos1[1] - pos2[1]) ** 2 + (pos1[2] - pos2[2]) ** 2
                    if d_sq < min_dist_sq:
                        min_dist_sq = d_sq
                        min_step_idx = step_idx

            if min_dist_sq < 5000.0 ** 2:
                conjunction_candidates.append((i, j, min_dist_sq, min_step_idx))

    events = []
    nearest_candidates = []

    # Cache for fine propagation: fine_cache[sat_index][(step_idx, k)] = (pos, vel)
    fine_cache = [{} for _ in range(num_sats)]

    def get_fine_state(sat_idx, step_idx, k):
        cache_key = (step_idx, k)
        if cache_key in fine_cache[sat_idx]:
            return fine_cache[sat_idx][cache_key]

        name, sat_rec, item = sat_data[sat_idx]
        center_dt = coarse_steps[step_idx][2]
        dt = center_dt + timedelta(minutes=k * 2)

        if sat_rec == "custom":
            launch_time = datetime.fromisoformat(item["launch_time"])
            r0 = [item["init_x"], item["init_y"], item["init_z"]]
            v0 = [item["init_vx"], item["init_vy"], item["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos, vel = propagate_keplerian_rk4(r0, v0, dt_sec)
        else:
            jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
            err, pos, vel = sat_rec.sgp4(jd, fr)
            if err != 0:
                pos, vel = None, None

        fine_cache[sat_idx][cache_key] = (pos, vel)
        return pos, vel

    for i, j, _, step_idx in conjunction_candidates:
        sat1_name, _, item1 = sat_data[i]
        sat2_name, _, item2 = sat_data[j]

        center_dt = coarse_steps[step_idx][2]
        fine_min_dist_sq = float("inf")
        fine_tca = center_dt
        fine_pos1 = None
        fine_pos2 = None
        fine_vel1 = None
        fine_vel2 = None

        for k in range(-22, 23):
            pos_vel1 = get_fine_state(i, step_idx, k)
            pos_vel2 = get_fine_state(j, step_idx, k)
            if pos_vel1[0] is not None and pos_vel2[0] is not None:
                pos1, vel1 = pos_vel1
                pos2, vel2 = pos_vel2
                d_sq = (pos1[0] - pos2[0]) ** 2 + (pos1[1] - pos2[1]) ** 2 + (pos1[2] - pos2[2]) ** 2
                if d_sq < fine_min_dist_sq:
                    fine_min_dist_sq = d_sq
                    fine_tca = center_dt + timedelta(minutes=k * 2)
                    fine_pos1 = pos1
                    fine_pos2 = pos2
                    fine_vel1 = vel1
                    fine_vel2 = vel2

        if fine_min_dist_sq == float("inf"):
            continue

        fine_min_dist = fine_min_dist_sq ** 0.5
        alt1 = (fine_pos1[0] ** 2 + fine_pos1[1] ** 2 + fine_pos1[2] ** 2) ** 0.5 - EARTH_RADIUS_KM
        alt2 = (fine_pos2[0] ** 2 + fine_pos2[1] ** 2 + fine_pos2[2] ** 2) ** 0.5 - EARTH_RADIUS_KM

        risk = risk_level(fine_min_dist)
        tca_from_now = (fine_tca - now).total_seconds() / 3600.0

        prob, xm, ym, C_2d = compute_pc_details_between_states(
            fine_pos1, fine_vel1, item1,
            fine_pos2, fine_vel2, item2
        )
        prob_str = format_probability_str(prob)

        event = {
            "sat1": sat1_name,
            "sat2": sat2_name,
            "distance_km": round(fine_min_dist, 3),
            "risk_level": risk,
            "alert": alert_message(sat1_name, sat2_name, fine_min_dist, risk),
            "sat1_alt": round(alt1, 1),
            "sat2_alt": round(alt2, 1),
            "tca": fine_tca.isoformat(),
            "tca_hours_from_now": round(tca_from_now, 2),
            "timestamp": now_iso(),
            "sat1_country": item1["country"],
            "sat1_type": item1["object_type"],
            "sat2_country": item2["country"],
            "sat2_type": item2["object_type"],
            "sat1_id": item1["norad_id"],
            "sat2_id": item2["norad_id"],
            "collision_probability": prob,
            "collision_probability_str": prob_str,
            "bplane_xm": xm,
            "bplane_ym": ym,
            "bplane_c2d": C_2d,
            "sat1_pos_tca": list(fine_pos1) if fine_pos1 else None,
            "sat1_vel_tca": list(fine_vel1) if fine_vel1 else None,
            "sat2_pos_tca": list(fine_pos2) if fine_pos2 else None,
            "sat2_vel_tca": list(fine_vel2) if fine_vel2 else None
        }


        if fine_min_dist <= threshold_km:
            events.append(event)
        nearest_candidates.append(event)

    events.sort(key=lambda x: x["distance_km"])
    nearest_candidates.sort(key=lambda x: x["distance_km"])

    return events, nearest_candidates[:20]


def log_operator_action(email: str, action: str, details: str) -> None:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    
    role = get_user_role(email)
    
    log_entry = {
        "timestamp": now_iso(),
        "email": email,
        "role": role,
        "action": action,
        "details": details
    }
    
    if url and token:
        try:
            requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["LPUSH", "audit:log", json.dumps(log_entry)],
                timeout=6,
            ).raise_for_status()
            
            requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["LTRIM", "audit:log", 0, 99],
                timeout=6,
            ).raise_for_status()
        except Exception:
            pass
    else:
        if "audit:log" not in _memory_store:
            _memory_store["audit:log"] = []
        _memory_store["audit:log"].insert(0, log_entry)
        _memory_store["audit:log"] = _memory_store["audit:log"][:100]


def log_conjunction_history(events: list[dict[str, Any]]) -> None:
    critical_events = [e for e in events if e["risk_level"] in ("CRITICAL", "HIGH")]
    if not critical_events:
        return
        
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    
    for event in critical_events:
        unique_id = f"{event['sat1']}:{event['sat2']}:{event['tca']}"
        
        if url and token:
            try:
                response = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=["SISMEMBER", "history:logged_set", unique_id],
                    timeout=6
                )
                response.raise_for_status()
                is_member = response.json().get("result")
                
                if not is_member:
                    requests.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json=["LPUSH", "history:conjunctions", json.dumps(event)],
                        timeout=6
                    ).raise_for_status()
                    requests.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json=["SADD", "history:logged_set", unique_id],
                        timeout=6
                    ).raise_for_status()
                    requests.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json=["LTRIM", "history:conjunctions", 0, 99],
                        timeout=6
                    ).raise_for_status()
            except Exception:
                pass
        else:
            if "history:logged_set" not in _memory_store:
                _memory_store["history:logged_set"] = set()
            if unique_id not in _memory_store["history:logged_set"]:
                _memory_store["history:logged_set"].add(unique_id)
                if "history:conjunctions" not in _memory_store:
                    _memory_store["history:conjunctions"] = []
                _memory_store["history:conjunctions"].insert(0, event)
                _memory_store["history:conjunctions"] = _memory_store["history:conjunctions"][:100]


def integration_status() -> dict[str, bool]:
    return {
        "celestrak": True,
        "spacetrack": bool(os.getenv("SPACETRACK_USER") and os.getenv("SPACETRACK_PASSWORD")),
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


def get_user_role(email: str) -> str:
    role = store_get(f"role:{email.lower()}")
    if not role:
        email_lower = email.lower()
        if "director" in email_lower:
            role = "Flight Director"
        elif "operator" in email_lower:
            role = "Operator"
        else:
            role = "Viewer"
        store_set(f"role:{email.lower()}", role)
    return role


def verify_session_and_role(authorization: str | None, required_roles: list[str]) -> str:
    email = verify_session(authorization)
    role = get_user_role(email)
    if role not in required_roles:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{role}' is unauthorized for this action. Required: {', '.join(required_roles)}"
        )
    return email


def rtn_to_eci_rotation(r: list[float], v: list[float]) -> list[list[float]]:
    r_mag = math.sqrt(r[0]**2 + r[1]**2 + r[2]**2)
    if r_mag == 0:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    uR = [x / r_mag for x in r]
    
    hx = r[1]*v[2] - r[2]*v[1]
    hy = r[2]*v[0] - r[0]*v[2]
    hz = r[0]*v[1] - r[1]*v[0]
    h_mag = math.sqrt(hx**2 + hy**2 + hz**2)
    if h_mag == 0:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    uN = [hx / h_mag, hy / h_mag, hz / h_mag]
    
    uTx = uN[1]*uR[2] - uN[2]*uR[1]
    uTy = uN[2]*uR[0] - uN[0]*uR[2]
    uTz = uN[0]*uR[1] - uN[1]*uR[0]
    uT = [uTx, uTy, uTz]
    
    return [
        [uR[0], uT[0], uN[0]],
        [uR[1], uT[1], uN[1]],
        [uR[2], uT[2], uN[2]]
    ]


def rotate_covariance_rtn_to_eci(r: list[float], v: list[float], r_err_km: float, t_err_km: float, n_err_km: float) -> list[list[float]]:
    rot = rtn_to_eci_rotation(r, v)
    sigmas = [r_err_km**2, t_err_km**2, n_err_km**2]
    cov = [[0.0]*3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            cov[i][j] = sum(rot[i][k] * sigmas[k] * rot[j][k] for k in range(3))
    return cov


def project_covariance_to_encounter_plane(C_eci: list[list[float]], ux: list[float], uy: list[float]) -> list[list[float]]:
    C_2d = [[0.0]*2 for _ in range(2)]
    P = [ux, uy]
    for i in range(2):
        for j in range(2):
            val = 0.0
            for a in range(3):
                for b in range(3):
                    val += P[i][a] * C_eci[a][b] * P[j][b]
            C_2d[i][j] = val
    return C_2d


def calculate_probability_of_collision(xm: float, ym: float, C_2d: list[list[float]], R_km: float = 0.015) -> float:
    det = C_2d[0][0]*C_2d[1][1] - C_2d[0][1]*C_2d[1][0]
    if det <= 0:
        dist = math.sqrt(xm**2 + ym**2)
        return 1.0 if dist <= R_km else 0.0
        
    inv00 = C_2d[1][1] / det
    inv01 = -C_2d[0][1] / det
    inv11 = C_2d[0][0] / det
    
    factor = 1.0 / (2.0 * math.pi * math.sqrt(det))
    
    r_steps = 10
    theta_steps = 20
    total_integral = 0.0
    
    dr = R_km / r_steps
    dtheta = (2.0 * math.pi) / theta_steps
    
    for ir in range(r_steps):
        r_val = (ir + 0.5) * dr
        for it in range(theta_steps):
            theta_val = it * dtheta
            x = r_val * math.cos(theta_val)
            y = r_val * math.sin(theta_val)
            dx = x - xm
            dy = y - ym
            quad = dx*dx*inv00 + 2.0*dx*dy*inv01 + dy*dy*inv11
            pdf = factor * math.exp(-0.5 * quad)
            total_integral += pdf * r_val * dr * dtheta
            
    return min(1.0, max(0.0, total_integral))


def compute_pc_details_between_states(pos1: list[float], vel1: list[float], item1: dict[str, Any], pos2: list[float], vel2: list[float], item2: dict[str, Any]) -> tuple[float, float, float, list[list[float]]]:
    def get_rtn_sigmas(item):
        if item.get("is_custom") or "radial_error_m" in item:
            sig_r = item.get("radial_error_m", 100.0) / 1000.0
            sig_t = item.get("transverse_error_m", 500.0) / 1000.0
            sig_n = item.get("normal_error_m", 200.0) / 1000.0
        else:
            obj_type = item.get("object_type", "PAYLOAD").upper()
            if "DEBRIS" in obj_type or "BODY" in obj_type or "ROCKET" in obj_type:
                sig_r = 0.25
                sig_t = 1.2
                sig_n = 0.5
            else:
                sig_r = 0.1
                sig_t = 0.5
                sig_n = 0.2
        return sig_r, sig_t, sig_n

    sig_r1, sig_t1, sig_n1 = get_rtn_sigmas(item1)
    C_eci1 = rotate_covariance_rtn_to_eci(pos1, vel1, sig_r1, sig_t1, sig_n1)

    sig_r2, sig_t2, sig_n2 = get_rtn_sigmas(item2)
    C_eci2 = rotate_covariance_rtn_to_eci(pos2, vel2, sig_r2, sig_t2, sig_n2)

    C_eci = [[C_eci1[a][b] + C_eci2[a][b] for b in range(3)] for a in range(3)]

    rel_pos = [pos2[0] - pos1[0], pos2[1] - pos1[1], pos2[2] - pos1[2]]
    rel_vel = [vel2[0] - vel1[0], vel2[1] - vel1[1], vel2[2] - vel1[2]]

    v_mag = math.sqrt(rel_vel[0]**2 + rel_vel[1]**2 + rel_vel[2]**2)
    if v_mag == 0:
        return 0.0, 0.0, 0.0, [[0.0, 0.0], [0.0, 0.0]]

    uz = [rel_vel[0]/v_mag, rel_vel[1]/v_mag, rel_vel[2]/v_mag]
    cx = rel_vel[1]*rel_pos[2] - rel_vel[2]*rel_pos[1]
    cy = rel_vel[2]*rel_pos[0] - rel_vel[0]*rel_pos[2]
    cz = rel_vel[0]*rel_pos[1] - rel_vel[1]*rel_pos[0]
    c_mag = math.sqrt(cx**2 + cy**2 + cz**2)

    if c_mag < 1e-9:
        if abs(uz[0]) > 0.9:
            uy = [0.0, 1.0, 0.0]
        else:
            uy = [1.0, 0.0, 0.0]
        dot = uy[0]*uz[0] + uy[1]*uz[1] + uy[2]*uz[2]
        uy = [uy[a] - dot*uz[a] for a in range(3)]
        uy_mag = math.sqrt(uy[0]**2 + uy[1]**2 + uy[2]**2)
        uy = [x / uy_mag for x in uy]
    else:
        uy = [cx / c_mag, cy / c_mag, cz / c_mag]

    ux = [uy[1]*uz[2] - uy[2]*uz[1], uy[2]*uz[0] - uy[0]*uz[2], uy[0]*uz[1] - uy[1]*uz[0]]

    xm = rel_pos[0]*ux[0] + rel_pos[1]*ux[1] + rel_pos[2]*ux[2]
    ym = rel_pos[0]*uy[0] + rel_pos[1]*uy[1] + rel_pos[2]*uy[2]

    C_2d = project_covariance_to_encounter_plane(C_eci, ux, uy)
    prob = calculate_probability_of_collision(xm, ym, C_2d, 0.015)
    return prob, xm, ym, C_2d


def compute_pc_between_states(pos1: list[float], vel1: list[float], item1: dict[str, Any], pos2: list[float], vel2: list[float], item2: dict[str, Any]) -> float:
    prob, _, _, _ = compute_pc_details_between_states(pos1, vel1, item1, pos2, vel2, item2)
    return prob


def compute_dilution_curve(xm: float, ym: float, C_2d: list[list[float]]) -> list[dict[str, Any]]:
    scales = [0.05, 0.1, 0.2, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0]
    curve = []
    for s in scales:
        scaled_C2d = [[C_2d[a][b] * (s**2) for b in range(2)] for a in range(2)]
        prob = calculate_probability_of_collision(xm, ym, scaled_C2d, 0.015)
        curve.append({"scale": s, "pc": prob})
    return curve


def format_probability_str(prob: float) -> str:
    if prob >= 1.0:
        return "1 in 1 (100.0%)"
    elif prob <= 0:
        return "0.0%"
    else:
        reciprocal = int(round(1.0 / prob))
        if reciprocal >= 1000000000:
            return f"1 in {reciprocal/1000000000:.1f}B"
        elif reciprocal >= 1000000:
            return f"1 in {reciprocal/1000000:.1f}M"
        else:
            return f"1 in {reciprocal:,}"





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

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": [
                    {"role": "system", "content": "You are OrbitWatch, a satellite collision-risk operations assistant. Write a concise operator briefing (maximum 3-4 sentences) outlining the current risk level, the top event, and recommended next action based on the stats and conjunctions provided."},
                    {"role": "user", "content": f"Stats: {json.dumps(stats_payload)} Events: {json.dumps(events[:5])}"}
                ],
                "max_tokens": 250,
                "temperature": 0.5,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        return {"mode": "openai_chat_api", "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "briefing": text}
    except Exception as e:
        return {
            "mode": "rules_fallback",
            "model": None,
            "briefing": f"Error calling OpenAI API: {str(e)}. Using deterministic fallback.",
        }


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
    authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    positions, meta = satellite_positions(group, limit)
    
    email = None
    if authorization:
        try:
            email = verify_session(authorization)
        except Exception:
            pass
            
    if email:
        custom_sats = get_custom_satellites_for_user(email)
        positions = custom_sats + positions
        
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
    authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    catalog, meta = fetch_telemetry_catalog(group, limit)
    
    email = None
    if authorization:
        try:
            email = verify_session(authorization)
        except Exception:
            pass
            
    if email:
        custom_sats = get_custom_satellites_for_user(email)
        catalog = custom_sats + catalog
        
    events, nearest = detect_time_series_conjunctions(catalog, threshold_km)
    log_conjunction_history(events)
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
    catalog, meta = fetch_telemetry_catalog(group, limit)
    events, nearest = detect_time_series_conjunctions(catalog, threshold_km)
    stats_payload = {"tracked_objects": len(catalog), "catalog": meta, "threshold_km": threshold_km}
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
    log_operator_action(payload.email, "AUTH_CODE_REQ", f"Requested operator login code. Sent status: {result.get('sent', False)}")
    return {
        "ok": True,
        "email": payload.email,
        "email_delivery": result,
        "dev_code": None if result.get("sent") else code,
    }


def add_registered_user(email: str) -> None:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        try:
            requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["SADD", "users:all", email],
                timeout=6
            )
        except Exception:
            pass
    else:
        if "users:all" not in _memory_store:
            _memory_store["users:all"] = set()
        _memory_store["users:all"].add(email)


def get_registered_users() -> list[str]:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["SMEMBERS", "users:all"],
                timeout=6
            )
            return resp.json().get("result", [])
        except Exception:
            return []
    else:
        return list(_memory_store.get("users:all", set()))


@app.post("/api/auth/verify-code")
def verify_code(payload: VerifyRequest) -> dict[str, Any]:
    record = store_get(f"login:{payload.email}")
    if not record or record.get("expires_at", 0) < time.time() or record.get("code") != payload.code:
        log_operator_action(payload.email, "AUTH_FAIL", "Invalid or expired login code attempted")
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    
    role = payload.requested_role
    if role not in ("Viewer", "Operator", "Flight Director"):
        role = get_user_role(payload.email)
    else:
        store_set(f"role:{payload.email.lower()}", role)
        
    store_set(f"user:{payload.email}", {"email": payload.email, "created_at": now_iso()})
    add_registered_user(payload.email)
    log_operator_action(payload.email, "AUTH_SUCCESS", f"Operator successfully logged in with role '{role}'")
    return {"ok": True, "token": sign_session(payload.email), "email": payload.email, "role": role}




@app.get("/api/user/watchlist")
def get_watchlist(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session(authorization)
    return {"email": email, "watchlist": store_get(f"watchlist:{email}", [])}


@app.post("/api/user/watchlist")
def add_watchlist(payload: WatchlistRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

    items = store_get(f"watchlist:{email}", [])
    item = {"object_name": payload.object_name, "threshold_km": payload.threshold_km, "created_at": now_iso()}
    items.append(item)
    store_set(f"watchlist:{email}", items)
    log_operator_action(email, "WATCHLIST_ADD", f"Added object '{payload.object_name}' to watchlist (threshold {payload.threshold_km} km)")
    return {"email": email, "watchlist": items}


@app.post("/api/alerts/test")
def send_test_alert(payload: AlertRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

    catalog, _meta = fetch_telemetry_catalog("STATIONS", 80)
    events, nearest = detect_time_series_conjunctions(catalog, payload.threshold_km)
    top = (events or nearest)[0] if (events or nearest) else None
    if not top:
        raise HTTPException(status_code=404, detail="No tracked approaches available")
    result = send_email(
        email,
        f"OrbitWatch {top['risk_level']} alert",
        f"<p>{top['alert']}</p><p>Min Distance: {top['distance_km']} km</p><p>TCA: {top['tca']} ({top['tca_hours_from_now']} hours from now)</p>",
    )
    log_operator_action(email, "ALERT_TEST", f"Dispatched conjunction test alert email to {email}. Status: {result.get('sent', False)}")
    return {"ok": True, "email": email, "top_event": top, "delivery": result}


@app.post("/api/chat")
def chat(
    payload: ChatRequest,
    authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")

    positions, meta = satellite_positions(payload.group, payload.limit)
    catalog, _ = fetch_telemetry_catalog(payload.group, payload.limit)
    events, nearest = detect_time_series_conjunctions(catalog, payload.threshold_km)

    email = None
    watchlist = []
    if authorization:
        try:
            email = verify_session(authorization)
            watchlist = store_get(f"watchlist:{email}", [])
        except Exception:
            pass

    stats_data = {
        "tracked_objects": len(positions),
        "active_satellites": len([sat for sat in positions if "DEB" not in sat["name"].upper()]),
        "debris_fragments": len([sat for sat in positions if "DEB" in sat["name"].upper()]),
        "high_risk_events": len([e for e in events if e["risk_level"] in ("CRITICAL", "HIGH")]),
        "catalog_source": meta["source"]
    }

    if not api_key:
        user_msg = payload.message.lower()
        words = user_msg.split()
        if "hello" in words or "hi" in words or "hey" in words:
            reply = "Hello! I am the OrbitWatch AI Co-pilot. Please configure an `OPENAI_API_KEY` to enable full chat reasoning. Currently running in offline rules-fallback mode."
        elif "conjunction" in user_msg or "risk" in user_msg or "collision" in user_msg:
            if events:
                top = events[0]
                reply = f"Currently, the highest risk conjunction is between **{top['sat1']}** and **{top['sat2']}** with a minimum distance of **{top['distance_km']} km** at TCA {top['tca_hours_from_now']} hours from now (Risk: {top['risk_level']})."
            else:
                reply = f"No conjunctions detected within the {payload.threshold_km} km threshold. The nearest approach is {nearest[0]['sat1']} and {nearest[0]['sat2']} at {nearest[0]['distance_km']} km."
        elif "stat" in user_msg or "how many" in user_msg:
            reply = f"Current stats: {stats_data['tracked_objects']} objects tracked (Active: {stats_data['active_satellites']}, Debris: {stats_data['debris_fragments']}) from {stats_data['catalog_source']}."
        elif "watchlist" in user_msg:
            if email:
                reply = f"Your watchlist contains {len(watchlist)} objects: " + ", ".join([w['object_name'] for w in watchlist])
            else:
                reply = "You are not logged in. Log in to track your satellite watchlist."
        else:
            reply = "I'm the OrbitWatch AI. To activate my full conversational reasoning capabilities, please set the `OPENAI_API_KEY` environment variable."

        return {"response": reply, "model": None}

    messages = [
        {
            "role": "system",
            "content": (
                "You are the OrbitWatch AI Co-pilot, an expert aerospace assistant helping operators monitor space debris and conjunction risks. "
                "You have access to the current system context:\n"
                f"- Live System Stats: {json.dumps(stats_data)}\n"
                f"- Top Conjunction Events (next 24h): {json.dumps(events[:5])}\n"
                f"- Current User Email: {email or 'Anonymous'}\n"
                f"- User Watchlist: {json.dumps(watchlist)}\n\n"
                "Answer the user's queries professionally, accurately, and concisely. "
                "If they ask about collision risks, explain the TCA (Time of Closest Approach) and CPA (Closest Point of Approach). "
                "If they ask about avoidance maneuvers, suggest retro-burns (to lower orbit/de-orbit or delay), radial-burns, or prograde-burns based on safety requirements."
            )
        }
    ]

    for msg in payload.history[-10:]:
        messages.append({"role": msg.role, "content": msg.content})

    messages.append({"role": "user", "content": payload.message})

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": messages,
                "max_tokens": 400,
                "temperature": 0.6,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        reply = data["choices"][0]["message"]["content"].strip()
        return {"response": reply, "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini")}
    except Exception as e:
        return {"response": f"Error communicating with OpenAI: {str(e)}", "model": None}


@app.get("/api/history/conjunctions")
def get_conjunction_history() -> dict[str, Any]:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    
    events = []
    if url and token:
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["LRANGE", "history:conjunctions", 0, -1],
                timeout=6
            )
            response.raise_for_status()
            raw_list = response.json().get("result", [])
            events = [json.loads(x) for x in raw_list]
        except Exception as e:
            return {"error": str(e), "history": []}
    else:
        events = _memory_store.get("history:conjunctions", [])
        
    return {"history": events}


@app.get("/api/history/audit")
def get_audit_history() -> dict[str, Any]:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    
    logs = []
    if url and token:
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["LRANGE", "audit:log", 0, -1],
                timeout=6
            )
            response.raise_for_status()
            raw_list = response.json().get("result", [])
            logs = [json.loads(x) for x in raw_list]
        except Exception as e:
            return {"error": str(e), "history": []}
    else:
        logs = _memory_store.get("audit:log", [])
        
    return {"history": logs}


def propagate_keplerian_rk4(r0: list[float], v0: list[float], dt_seconds: float) -> tuple[list[float], list[float]]:
    mu = 398600.4418
    
    def derivatives(state):
        rx, ry, rz, vx, vy, vz = state
        r = math.sqrt(rx*rx + ry*ry + rz*rz)
        if r == 0:
            return [0.0]*6
        ax = -mu * rx / (r*r*r)
        ay = -mu * ry / (r*r*r)
        az = -mu * rz / (r*r*r)
        return [vx, vy, vz, ax, ay, az]
        
    state = [r0[0], r0[1], r0[2], v0[0], v0[1], v0[2]]
    
    h = 30.0
    steps = abs(int(dt_seconds / h))
    if steps == 0:
        steps = 1
    h_step = math.copysign(h, dt_seconds)
    remaining_time = dt_seconds - (h_step * steps)
    
    for _ in range(steps):
        k1 = derivatives(state)
        state_k2 = [state[i] + 0.5 * h_step * k1[i] for i in range(6)]
        k2 = derivatives(state_k2)
        state_k3 = [state[i] + 0.5 * h_step * k2[i] for i in range(6)]
        k3 = derivatives(state_k3)
        state_k4 = [state[i] + h_step * k3[i] for i in range(6)]
        k4 = derivatives(state_k4)
        state = [state[i] + (h_step / 6.0) * (k1[i] + 2*k2[i] + 2*k3[i] + k4[i]) for i in range(6)]
        
    if abs(remaining_time) > 0.001:
        k1 = derivatives(state)
        state_k2 = [state[i] + 0.5 * remaining_time * k1[i] for i in range(6)]
        k2 = derivatives(state_k2)
        state_k3 = [state[i] + 0.5 * remaining_time * k2[i] for i in range(6)]
        k3 = derivatives(state_k3)
        state_k4 = [state[i] + remaining_time * k3[i] for i in range(6)]
        k4 = derivatives(state_k4)
        state = [state[i] + (remaining_time / 6.0) * (k1[i] + 2*k2[i] + 2*k3[i] + k4[i]) for i in range(6)]
        
    return state[:3], state[3:]


@app.post("/api/maneuver/simulate")
def simulate_avoidance_maneuver(
    payload: ManeuverRequest,
    authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

    custom_sats = get_custom_satellites_for_user(email)
    
    sat1_custom = next((s for s in custom_sats if s["name"] == payload.sat1_name), None)
    sat2_custom = next((s for s in custom_sats if s["name"] == payload.sat2_name), None)
    
    sat1_item = None
    sat2_item = None
    
    if sat1_custom:
        sat1_item = sat1_custom
    else:
        catalog, _ = fetch_telemetry_catalog(payload.group, payload.limit)
        sat1_item = next((item for item in catalog if item["name"] == payload.sat1_name), None)
        if not sat1_item:
            demo_item = next((t for t in DEMO_TLES if t[0] == payload.sat1_name), None)
            if demo_item:
                sat1_item = map_celestrak_to_dict(demo_item[0], demo_item[1], demo_item[2])
                
    if sat2_custom:
        sat2_item = sat2_custom
    else:
        catalog, _ = fetch_telemetry_catalog(payload.group, payload.limit)
        sat2_item = next((item for item in catalog if item["name"] == payload.sat2_name), None)
        if not sat2_item:
            demo_item = next((t for t in DEMO_TLES if t[0] == payload.sat2_name), None)
            if demo_item:
                sat2_item = map_celestrak_to_dict(demo_item[0], demo_item[1], demo_item[2])
                
    if not sat1_item or not sat2_item:
        raise HTTPException(status_code=404, detail="One or both satellites not found in catalog")
        
    sat1_rec = Satrec.twoline2rv(sat1_item["tle1"], sat1_item["tle2"]) if not sat1_custom else None
    sat2_rec = Satrec.twoline2rv(sat2_item["tle1"], sat2_item["tle2"]) if not sat2_custom else None
    
    now = datetime.now(timezone.utc)
    min_dist = float("inf")
    tca_dt = now
    
    for i in range(25):
        dt = now + timedelta(hours=i)
        jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        
        if sat1_custom:
            launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
            r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
            v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos1, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err1 = 0
        else:
            err1, pos1, _ = sat1_rec.sgp4(jd, fr)
            
        if sat2_custom:
            launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
            r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
            v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos2, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err2 = 0
        else:
            err2, pos2, _ = sat2_rec.sgp4(jd, fr)
            
        if err1 == 0 and err2 == 0 and pos1 is not None and pos2 is not None:
            d = math.sqrt((pos1[0]-pos2[0])**2 + (pos1[1]-pos2[1])**2 + (pos1[2]-pos2[2])**2)
            if d < min_dist:
                min_dist = d
                tca_dt = dt
                
    fine_min_dist = float("inf")
    tca_precise_dt = tca_dt
    for k in range(-45, 46):
        dt = tca_dt + timedelta(minutes=k)
        jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        
        if sat1_custom:
            launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
            r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
            v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos1, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err1 = 0
        else:
            err1, pos1, _ = sat1_rec.sgp4(jd, fr)
            
        if sat2_custom:
            launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
            r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
            v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos2, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err2 = 0
        else:
            err2, pos2, _ = sat2_rec.sgp4(jd, fr)
            
        if err1 == 0 and err2 == 0 and pos1 is not None and pos2 is not None:
            d = math.sqrt((pos1[0]-pos2[0])**2 + (pos1[1]-pos2[1])**2 + (pos1[2]-pos2[2])**2)
            if d < fine_min_dist:
                fine_min_dist = d
                tca_precise_dt = dt
                
    # Re-propagate to get original states (pos, vel) at precise TCA
    jd_tca, fr_tca = jday(tca_precise_dt.year, tca_precise_dt.month, tca_precise_dt.day, tca_precise_dt.hour, tca_precise_dt.minute, tca_precise_dt.second)
    if sat1_custom:
        launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
        r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
        v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
        dt_sec = (tca_precise_dt - launch_time).total_seconds()
        pos1_tca, vel1_tca = propagate_keplerian_rk4(r0, v0, dt_sec)
    else:
        _, pos1_tca, vel1_tca = sat1_rec.sgp4(jd_tca, fr_tca)

    if sat2_custom:
        launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
        r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
        v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
        dt_sec = (tca_precise_dt - launch_time).total_seconds()
        pos2_tca, vel2_tca = propagate_keplerian_rk4(r0, v0, dt_sec)
    else:
        _, pos2_tca, vel2_tca = sat2_rec.sgp4(jd_tca, fr_tca)
        
    if pos1_tca is None or pos2_tca is None or vel1_tca is None or vel2_tca is None:
        raise HTTPException(status_code=400, detail="Could not propagate orbital states to precise TCA")

    t_burn = tca_precise_dt - timedelta(hours=payload.burn_hours_before_tca)
    if t_burn < now:
        t_burn = now
        
    jd_b, fr_b = jday(t_burn.year, t_burn.month, t_burn.day, t_burn.hour, t_burn.minute, t_burn.second)
    
    if sat1_custom:
        launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
        r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
        v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
        dt_sec = (t_burn - launch_time).total_seconds()
        pos1_b, vel1_b = propagate_keplerian_rk4(r0, v0, dt_sec)
        err1 = 0
    else:
        err1, pos1_b, vel1_b = sat1_rec.sgp4(jd_b, fr_b)
        
    if sat2_custom:
        launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
        r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
        v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
        dt_sec = (t_burn - launch_time).total_seconds()
        pos2_b, vel2_b = propagate_keplerian_rk4(r0, v0, dt_sec)
        err2 = 0
    else:
        err2, pos2_b, vel2_b = sat2_rec.sgp4(jd_b, fr_b)
        
    if err1 != 0 or err2 != 0 or pos1_b is None or pos2_b is None:
        raise HTTPException(status_code=400, detail="Could not propagate orbit to selected burn epoch")
        
    is_sat1 = payload.burn_satellite == "sat1"
    burned_sat_name = payload.sat1_name if is_sat1 else payload.sat2_name
    
    r_b = list(pos1_b) if is_sat1 else list(pos2_b)
    v_b = list(vel1_b) if is_sat1 else list(vel2_b)
    
    v_mag = math.sqrt(v_b[0]**2 + v_b[1]**2 + v_b[2]**2)
    r_mag = math.sqrt(r_b[0]**2 + r_b[1]**2 + r_b[2]**2)
    
    if v_mag == 0 or r_mag == 0:
        raise HTTPException(status_code=400, detail="Satellite state vectors are singular")
        
    u = [0.0, 0.0, 0.0]
    if payload.burn_direction == "prograde":
        u = [x / v_mag for x in v_b]
    elif payload.burn_direction == "retrograde":
        u = [-x / v_mag for x in v_b]
    elif payload.burn_direction == "radial":
        u = [x / r_mag for x in r_b]
        
    dv_km_s = payload.delta_v_m_s / 1000.0
    v_b_new = [v_b[i] + dv_km_s * u[i] for i in range(3)]
    
    dt_seconds = (tca_precise_dt - t_burn).total_seconds()
    r_burned_tca, v_burned_tca = propagate_keplerian_rk4(r_b, v_b_new, dt_seconds)
    
    if is_sat1:
        r_unburned_tca = pos2_tca
        pos1_sim_tca, vel1_sim_tca = r_burned_tca, v_burned_tca
        pos2_sim_tca, vel2_sim_tca = pos2_tca, vel2_tca
    else:
        r_unburned_tca = pos1_tca
        pos1_sim_tca, vel1_sim_tca = pos1_tca, vel1_tca
        pos2_sim_tca, vel2_sim_tca = r_burned_tca, v_burned_tca
        
    sim_dist = math.sqrt((r_burned_tca[0]-r_unburned_tca[0])**2 + (r_burned_tca[1]-r_unburned_tca[1])**2 + (r_burned_tca[2]-r_unburned_tca[2])**2)
    
    original_collision_probability, orig_xm, orig_ym, orig_c2d = compute_pc_details_between_states(
        pos1_tca, vel1_tca, sat1_item,
        pos2_tca, vel2_tca, sat2_item
    )
    original_collision_probability_str = format_probability_str(original_collision_probability)
    original_dilution_curve = compute_dilution_curve(orig_xm, orig_ym, orig_c2d)
    
    simulated_collision_probability, sim_xm, sim_ym, sim_c2d = compute_pc_details_between_states(
        pos1_sim_tca, vel1_sim_tca, sat1_item,
        pos2_sim_tca, vel2_sim_tca, sat2_item
    )
    simulated_collision_probability_str = format_probability_str(simulated_collision_probability)
    simulated_dilution_curve = compute_dilution_curve(sim_xm, sim_ym, sim_c2d)

    time_series = []
    for step in range(11):
        fraction = step / 10.0
        step_dt = t_burn + timedelta(seconds=dt_seconds * fraction)
        
        jd_s, fr_s = jday(step_dt.year, step_dt.month, step_dt.day, step_dt.hour, step_dt.minute, step_dt.second)
        
        if sat1_custom:
            launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
            r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
            v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
            dt_sec_s = (step_dt - launch_time).total_seconds()
            r1_orig, _ = propagate_keplerian_rk4(r0, v0, dt_sec_s)
            err1_s = 0
        else:
            err1_s, r1_orig, _ = sat1_rec.sgp4(jd_s, fr_s)
            
        if sat2_custom:
            launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
            r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
            v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
            dt_sec_s = (step_dt - launch_time).total_seconds()
            r2_orig, _ = propagate_keplerian_rk4(r0, v0, dt_sec_s)
            err2_s = 0
        else:
            err2_s, r2_orig, _ = sat2_rec.sgp4(jd_s, fr_s)
            
        orig_d = float('inf')
        if err1_s == 0 and err2_s == 0 and r1_orig is not None and r2_orig is not None:
            orig_d = math.sqrt((r1_orig[0]-r2_orig[0])**2 + (r1_orig[1]-r2_orig[1])**2 + (r1_orig[2]-r2_orig[2])**2)
            
        step_dt_seconds = dt_seconds * fraction
        r_burn_step, _ = propagate_keplerian_rk4(r_b, v_b_new, step_dt_seconds)
        r_unburned_step = r2_orig if is_sat1 else r1_orig
        err_unburned_step = err2_s if is_sat1 else err1_s
        
        sim_d = float('inf')
        if r_burn_step is not None and err_unburned_step == 0 and r_unburned_step is not None:
            sim_d = math.sqrt((r_burn_step[0]-r_unburned_step[0])**2 + (r_burn_step[1]-r_unburned_step[1])**2 + (r_burn_step[2]-r_unburned_step[2])**2)
            
        time_series.append({
            "step": step,
            "hours_from_burn": round(step_dt_seconds / 3600.0, 2),
            "original_distance_km": round(orig_d, 3) if orig_d != float('inf') else None,
            "simulated_distance_km": round(sim_d, 3) if sim_d != float('inf') else None
        })
        
    operator_email = email if email else "operator"
    log_operator_action(
        operator_email, 
        "BURN_SIMULATE", 
        f"Simulated {payload.burn_direction} burn on '{burned_sat_name}' (Delta-V: {payload.delta_v_m_s} m/s) at {payload.burn_hours_before_tca}h before TCA. Separation resolved from {fine_min_dist:.2f} km to {sim_dist:.2f} km."
    )
    
    outcome = "COLLISION AVOIDED" if sim_dist > 10.0 else "RISK STILL CRITICAL"
    
    return {
        "sat1_name": payload.sat1_name,
        "sat2_name": payload.sat2_name,
        "burn_satellite": payload.burn_satellite,
        "burn_direction": payload.burn_direction,
        "delta_v_m_s": payload.delta_v_m_s,
        "burn_hours_before_tca": payload.burn_hours_before_tca,
        "original_distance_km": round(fine_min_dist, 3),
        "simulated_distance_km": round(sim_dist, 3),
        "delta_distance_km": round(sim_dist - fine_min_dist, 3),
        "tca": tca_precise_dt.isoformat(),
        "outcome": outcome,
        "time_series": time_series,
        "original_collision_probability": original_collision_probability,
        "original_collision_probability_str": original_collision_probability_str,
        "simulated_collision_probability": simulated_collision_probability,
        "simulated_collision_probability_str": simulated_collision_probability_str,
        "original_bplane": {"xm": orig_xm, "ym": orig_ym, "c2d": orig_c2d},
        "simulated_bplane": {"xm": sim_xm, "ym": sim_ym, "c2d": sim_c2d},
        "original_dilution_curve": original_dilution_curve,
        "simulated_dilution_curve": simulated_dilution_curve,
        "burn_epoch": t_burn.isoformat(),
        "burn_satellite_pos_burn": list(r_b) if r_b else None,
        "burn_satellite_vel_burn_orig": list(v_b) if v_b else None,
        "burn_satellite_vel_burn_post": list(v_b_new) if v_b_new else None,
        "unburned_satellite_pos_burn": list(pos2_b) if is_sat1 else list(pos1_b),
        "unburned_satellite_vel_burn": list(vel2_b) if is_sat1 else list(vel1_b),
        "sat1_pos_tca": list(pos1_tca) if pos1_tca else None,
        "sat1_vel_tca": list(vel1_tca) if vel1_tca else None,
        "sat2_pos_tca": list(pos2_tca) if pos2_tca else None,
        "sat2_vel_tca": list(vel2_tca) if vel2_tca else None,
        "sat1_sim_pos_tca": list(pos1_sim_tca) if pos1_sim_tca else None,
        "sat1_sim_vel_tca": list(vel1_sim_tca) if vel1_sim_tca else None,
        "sat2_sim_pos_tca": list(pos2_sim_tca) if pos2_sim_tca else None,
        "sat2_sim_vel_tca": list(vel2_sim_tca) if vel2_sim_tca else None
    }


class AlertSettingsRequest(BaseModel):
    enabled: bool
    email: EmailStr
    threshold_level: str


@app.get("/api/alerts/settings")
def get_alert_settings(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session(authorization)
    settings = store_get(f"settings:alerts:{email}", {
        "enabled": False,
        "email": email,
        "threshold_level": "CRITICAL"
    })
    return settings


@app.post("/api/alerts/settings")
def save_alert_settings(payload: AlertSettingsRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

    settings = {
        "enabled": payload.enabled,
        "email": payload.email,
        "threshold_level": payload.threshold_level
    }
    store_set(f"settings:alerts:{email}", settings)
    log_operator_action(email, "ALERT_SETTINGS_UPDATE", f"Alerts: {'ENABLED' if payload.enabled else 'DISABLED'}, target: {payload.email}, threshold: {payload.threshold_level}")
    return {"ok": True, "settings": settings}


def matches_alert_threshold(risk: str, threshold: str) -> bool:
    levels = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    try:
        risk_idx = levels.index(risk)
        thresh_idx = levels.index(threshold)
        return risk_idx >= thresh_idx
    except ValueError:
        return False


def perform_scan_for_user(email: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    if not settings.get("enabled"):
        return []
        
    target_email = settings.get("email")
    threshold = settings.get("threshold_level", "CRITICAL")
    
    catalog, _ = fetch_telemetry_catalog("STATIONS", 80)
    events, nearest = detect_time_series_conjunctions(catalog, 2000.0)
    
    all_events = events if events else nearest
    matched_events = []
    
    for event in all_events:
        if matches_alert_threshold(event["risk_level"], threshold):
            unique_id = f"{event['sat1']}:{event['sat2']}:{event['tca']}"
            emailed_key = f"history:emailed_set:{email}"
            
            url = os.getenv("UPSTASH_REDIS_REST_URL")
            token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
            is_emailed = False
            if url and token:
                try:
                    resp = requests.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        json=["SISMEMBER", emailed_key, unique_id],
                        timeout=6
                    )
                    is_emailed = resp.json().get("result")
                except Exception:
                    pass
            else:
                if emailed_key not in _memory_store:
                    _memory_store[emailed_key] = set()
                is_emailed = unique_id in _memory_store[emailed_key]
                
            if not is_emailed:
                if url and token:
                    try:
                        requests.post(
                            url,
                            headers={"Authorization": f"Bearer {token}"},
                            json=["SADD", emailed_key, unique_id],
                            timeout=6
                        )
                    except Exception:
                        pass
                else:
                    _memory_store[emailed_key].add(unique_id)
                    
                html_body = f"""
                <h2>OrbitWatch Conjunction Proximity Alert</h2>
                <p>The system detected a proximity threat matching your threshold (<strong>{threshold}</strong>):</p>
                <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; font-family:sans-serif; font-size:14px;">
                    <tr style="background:#f1f5f9;">
                        <th>Object 1</th>
                        <th>Object 2</th>
                        <th>Distance (km)</th>
                        <th>TCA (hours from now)</th>
                        <th>Risk Level</th>
                    </tr>
                    <tr>
                        <td><strong>{event['sat1']}</strong><br><span style="color:#64748b; font-size:12px;">ID: {event['sat1_id']} | Alt: {event['sat1_alt']}km | {event['sat1_country']}</span></td>
                        <td><strong>{event['sat2']}</strong><br><span style="color:#64748b; font-size:12px;">ID: {event['sat2_id']} | Alt: {event['sat2_alt']}km | {event['sat2_country']}</span></td>
                        <td style="color:#e11d48; font-weight:bold;">{event['distance_km']} km</td>
                        <td>{event['tca_hours_from_now']} hrs</td>
                        <td style="font-weight:bold;">{event['risk_level']}</td>
                    </tr>
                </table>
                <p><strong>Operator Guidance:</strong> {event['alert']}</p>
                """
                send_email(
                    target_email,
                    f"OrbitWatch {event['risk_level']} Alert: {event['sat1']} - {event['sat2']}",
                    html_body
                )
                
                log_operator_action(
                    email, 
                    "AUTO_ALERT_SENT", 
                    f"Dispatched automated proximity email to {target_email} for {event['sat1']} ↔ {event['sat2']} (Risk: {event['risk_level']})"
                )
                matched_events.append(event)
                
    return matched_events


@app.post("/api/alerts/scan")
def trigger_manual_scan(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

    settings = store_get(f"settings:alerts:{email}", {
        "enabled": False,
        "email": email,
        "threshold_level": "CRITICAL"
    })
    
    scan_settings = dict(settings)
    scan_settings["enabled"] = True
    
    sent_alerts = perform_scan_for_user(email, scan_settings)
    log_operator_action(email, "MANUAL_SCAN_TRIGGER", f"Triggered manual scan. Sent {len(sent_alerts)} new alerts.")
    return {"ok": True, "sent_count": len(sent_alerts), "sent_alerts": sent_alerts}


def run_background_scanner() -> None:
    import time
    while True:
        try:
            time.sleep(300)
            users = get_registered_users()
            for user in users:
                settings = store_get(f"settings:alerts:{user}")
                if settings and settings.get("enabled"):
                    perform_scan_for_user(user, settings)
        except Exception:
            pass


@app.on_event("startup")
def start_background_scanner_task() -> None:
    import threading
    thread = threading.Thread(target=run_background_scanner, daemon=True)
    thread.start()


class CustomSatelliteRequest(BaseModel):
    name: str
    semi_major_axis_km: float
    eccentricity: float
    inclination_deg: float
    raan_deg: float
    arg_perigee_deg: float
    mean_anomaly_deg: float
    country: str = "USA"
    radial_error_m: float = 100.0
    transverse_error_m: float = 500.0
    normal_error_m: float = 200.0



def keplerian_to_cartesian(
    a: float, e: float, i_deg: float, raan_deg: float, arg_pe_deg: float, M_deg: float
) -> tuple[list[float], list[float]]:
    mu = 398600.4418 # km^3 / s^2
    
    i = math.radians(i_deg)
    raan = math.radians(raan_deg)
    arg_pe = math.radians(arg_pe_deg)
    M = math.radians(M_deg)
    
    # Solve Kepler's Equation
    E = M
    for _ in range(15):
        f_val = E - e * math.sin(E) - M
        df_val = 1.0 - e * math.cos(E)
        if abs(df_val) < 1e-12:
            break
        dE = f_val / df_val
        E = E - dE
        if abs(dE) < 1e-8:
            break
            
    sin_nu = (math.sqrt(1.0 - e*e) * math.sin(E)) / (1.0 - e * math.cos(E))
    cos_nu = (math.cos(E) - e) / (1.0 - e * math.cos(E))
    nu = math.atan2(sin_nu, cos_nu)
    
    r = a * (1.0 - e * math.cos(E))
    
    p = a * (1.0 - e*e)
    if p <= 0:
        p = 1e-6
    
    r_PQW = [r * math.cos(nu), r * math.sin(nu), 0.0]
    
    sqrt_mu_p = math.sqrt(mu / p)
    v_PQW = [-sqrt_mu_p * math.sin(nu), sqrt_mu_p * (e + math.cos(nu)), 0.0]
    
    cos_w = math.cos(arg_pe)
    sin_w = math.sin(arg_pe)
    cos_O = math.cos(raan)
    sin_O = math.sin(raan)
    cos_i = math.cos(i)
    sin_i = math.sin(i)
    
    x = r_PQW[0] * (cos_w * cos_O - sin_w * sin_O * cos_i) - r_PQW[1] * (sin_w * cos_O + cos_w * sin_O * cos_i)
    y = r_PQW[0] * (cos_w * sin_O + sin_w * cos_O * cos_i) - r_PQW[1] * (sin_w * sin_O - cos_w * cos_O * cos_i)
    z = r_PQW[0] * (sin_w * sin_i) + r_PQW[1] * (cos_w * sin_i)
    
    vx = v_PQW[0] * (cos_w * cos_O - sin_w * sin_O * cos_i) - v_PQW[1] * (sin_w * cos_O + cos_w * sin_O * cos_i)
    vy = v_PQW[0] * (cos_w * sin_O + sin_w * cos_O * cos_i) - v_PQW[1] * (sin_w * sin_O - cos_w * cos_O * cos_i)
    vz = v_PQW[0] * (sin_w * sin_i) + v_PQW[1] * (cos_w * sin_i)
    
    return [x, y, z], [vx, vy, vz]


def get_custom_satellites_for_user(email: str | None) -> list[dict[str, Any]]:
    if not email:
        return []
    sats = store_get(f"custom_sats:{email}", [])
    
    now = datetime.now(timezone.utc)
    propagated_sats = []
    
    for sat in sats:
        try:
            launch_time = datetime.fromisoformat(sat["launch_time"])
            dt_seconds = (now - launch_time).total_seconds()
            
            r0 = [sat["init_x"], sat["init_y"], sat["init_z"]]
            v0 = [sat["init_vx"], sat["init_vy"], sat["init_vz"]]
            
            r_t, v_t = propagate_keplerian_rk4(r0, v0, dt_seconds)
            
            radius = math.sqrt(r_t[0]**2 + r_t[1]**2 + r_t[2]**2)
            speed = math.sqrt(v_t[0]**2 + v_t[1]**2 + v_t[2]**2)
            
            propagated_sats.append({
                "name": sat["name"],
                "x": round(r_t[0], 3),
                "y": round(r_t[1], 3),
                "z": round(r_t[2], 3),
                "vx": round(v_t[0], 6),
                "vy": round(v_t[1], 6),
                "vz": round(v_t[2], 6),
                "radius_km": round(radius, 3),
                "alt_km": round(radius - EARTH_RADIUS_KM, 1),
                "speed_km_s": round(speed, 4),
                "frame": "TEME",
                "country": sat["country"],
                "object_type": "PAYLOAD",
                "norad_id": sat["norad_id"],
                "is_custom": True,
                "launch_time": sat["launch_time"],
                "init_x": sat["init_x"],
                "init_y": sat["init_y"],
                "init_z": sat["init_z"],
                "init_vx": sat["init_vx"],
                "init_vy": sat["init_vy"],
                "init_vz": sat["init_vz"],
                "radial_error_m": sat.get("radial_error_m", 100.0),
                "transverse_error_m": sat.get("transverse_error_m", 500.0),
                "normal_error_m": sat.get("normal_error_m", 200.0)
            })

        except Exception:
            continue
            
    return propagated_sats


@app.post("/api/user/custom-satellites")
def launch_custom_satellite(payload: CustomSatelliteRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

    
    pos, vel = keplerian_to_cartesian(
        payload.semi_major_axis_km,
        payload.eccentricity,
        payload.inclination_deg,
        payload.raan_deg,
        payload.arg_perigee_deg,
        payload.mean_anomaly_deg
    )
    
    norad_id = f"C-{secrets.randbelow(100000):05d}"
    
    sat_entry = {
        "name": payload.name,
        "country": payload.country.upper(),
        "norad_id": norad_id,
        "launch_time": now_iso(),
        "init_x": pos[0],
        "init_y": pos[1],
        "init_z": pos[2],
        "init_vx": vel[0],
        "init_vy": vel[1],
        "init_vz": vel[2],
        "radial_error_m": payload.radial_error_m,
        "transverse_error_m": payload.transverse_error_m,
        "normal_error_m": payload.normal_error_m
    }

    
    items = store_get(f"custom_sats:{email}", [])
    items.append(sat_entry)
    store_set(f"custom_sats:{email}", items)
    
    log_operator_action(
        email, 
        "LAUNCH_CUSTOM_SAT", 
        f"Launched custom satellite '{payload.name}' (ID: {norad_id}, a={payload.semi_major_axis_km}km, i={payload.inclination_deg}deg)"
    )
    
    return {"ok": True, "satellite": sat_entry}


@app.post("/api/maneuver/solve")
def solve_avoidance_maneuver(
    payload: ManeuverSolveRequest,
    authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Operator", "Flight Director"])

            
    custom_sats = get_custom_satellites_for_user(email)
    
    sat1_custom = next((s for s in custom_sats if s["name"] == payload.sat1_name), None)
    sat2_custom = next((s for s in custom_sats if s["name"] == payload.sat2_name), None)
    
    sat1_tle = None
    sat2_tle = None
    if not sat1_custom:
        tles, _ = fetch_celestrak_tles(payload.group, payload.limit)
        sat1_tle = next((t for t in tles if t[0] == payload.sat1_name), None)
        if not sat1_tle:
            sat1_tle = next((t for t in DEMO_TLES if t[0] == payload.sat1_name), None)
            
    if not sat2_custom:
        tles, _ = fetch_celestrak_tles(payload.group, payload.limit)
        sat2_tle = next((t for t in tles if t[0] == payload.sat2_name), None)
        if not sat2_tle:
            sat2_tle = next((t for t in DEMO_TLES if t[0] == payload.sat2_name), None)
            
    if (not sat1_custom and not sat1_tle) or (not sat2_custom and not sat2_tle):
        raise HTTPException(status_code=404, detail="One or both satellites not found in catalog")
        
    sat1_rec = Satrec.twoline2rv(sat1_tle[1], sat1_tle[2]) if sat1_tle else None
    sat2_rec = Satrec.twoline2rv(sat2_tle[1], sat2_tle[2]) if sat2_tle else None
    
    now = datetime.now(timezone.utc)
    
    # 1. Find unburned TCA first
    min_dist = float("inf")
    tca_dt = now
    
    for i in range(25):
        dt = now + timedelta(hours=i)
        jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        
        if sat1_custom:
            launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
            r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
            v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos1, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err1 = 0
        else:
            err1, pos1, _ = sat1_rec.sgp4(jd, fr)
            
        if sat2_custom:
            launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
            r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
            v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos2, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err2 = 0
        else:
            err2, pos2, _ = sat2_rec.sgp4(jd, fr)
            
        if err1 == 0 and err2 == 0 and pos1 is not None and pos2 is not None:
            d = math.sqrt((pos1[0]-pos2[0])**2 + (pos1[1]-pos2[1])**2 + (pos1[2]-pos2[2])**2)
            if d < min_dist:
                min_dist = d
                tca_dt = dt
                
    fine_min_dist = float("inf")
    tca_precise_dt = tca_dt
    for k in range(-45, 46):
        dt = tca_dt + timedelta(minutes=k)
        jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
        
        if sat1_custom:
            launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
            r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
            v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos1, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err1 = 0
        else:
            err1, pos1, _ = sat1_rec.sgp4(jd, fr)
            
        if sat2_custom:
            launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
            r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
            v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
            dt_sec = (dt - launch_time).total_seconds()
            pos2, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
            err2 = 0
        else:
            err2, pos2, _ = sat2_rec.sgp4(jd, fr)
            
        if err1 == 0 and err2 == 0 and pos1 is not None and pos2 is not None:
            d = math.sqrt((pos1[0]-pos2[0])**2 + (pos1[1]-pos2[1])**2 + (pos1[2]-pos2[2])**2)
            if d < fine_min_dist:
                fine_min_dist = d
                tca_precise_dt = dt

    # 2. Run the Parameter Sweep to optimize avoidance burn
    best_burn = None
    best_sim_dist = -1.0
    
    safety_threshold = 10.0
    
    sats_to_burn = []
    if not sat1_custom and sat1_tle:
        is_deb = "DEB" in payload.sat1_name.upper() or "R/B" in payload.sat1_name.upper() or "ROCKET" in payload.sat1_name.upper()
        if not is_deb:
            sats_to_burn.append("sat1")
    else:
        sats_to_burn.append("sat1")
        
    if not sat2_custom and sat2_tle:
        is_deb = "DEB" in payload.sat2_name.upper() or "R/B" in payload.sat2_name.upper() or "ROCKET" in payload.sat2_name.upper()
        if not is_deb:
            sats_to_burn.append("sat2")
    else:
        sats_to_burn.append("sat2")
        
    if not sats_to_burn:
        sats_to_burn = ["sat1"]
        
    lead_times = [1.0, 2.0, 4.0, 8.0]
    directions = ["retrograde", "prograde", "radial"]
    dv_values = [round(0.1 + x * 0.2, 2) for x in range(25)]
    
    for sat_choice in sats_to_burn:
        for lead_h in lead_times:
            t_burn = tca_precise_dt - timedelta(hours=lead_h)
            if t_burn < now:
                t_burn = now
                
            jd_b, fr_b = jday(t_burn.year, t_burn.month, t_burn.day, t_burn.hour, t_burn.minute, t_burn.second)
            
            if sat1_custom:
                launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
                r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
                v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
                dt_sec = (t_burn - launch_time).total_seconds()
                pos1_b, vel1_b = propagate_keplerian_rk4(r0, v0, dt_sec)
                err1 = 0
            else:
                err1, pos1_b, vel1_b = sat1_rec.sgp4(jd_b, fr_b)
                
            if sat2_custom:
                launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
                r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
                v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
                dt_sec = (t_burn - launch_time).total_seconds()
                pos2_b, vel2_b = propagate_keplerian_rk4(r0, v0, dt_sec)
                err2 = 0
            else:
                err2, pos2_b, vel2_b = sat2_rec.sgp4(jd_b, fr_b)
                
            if err1 != 0 or err2 != 0 or pos1_b is None or pos2_b is None:
                continue
                
            is_sat1 = (sat_choice == "sat1")
            r_b = list(pos1_b) if is_sat1 else list(pos2_b)
            v_b = list(vel1_b) if is_sat1 else list(vel2_b)
            
            v_mag = math.sqrt(v_b[0]**2 + v_b[1]**2 + v_b[2]**2)
            r_mag = math.sqrt(r_b[0]**2 + r_b[1]**2 + r_b[2]**2)
            if v_mag == 0 or r_mag == 0:
                continue
                
            dir_vectors = {}
            dir_vectors["prograde"] = [x / v_mag for x in v_b]
            dir_vectors["retrograde"] = [-x / v_mag for x in v_b]
            dir_vectors["radial"] = [x / r_mag for x in r_b]
            
            dt_seconds = (tca_precise_dt - t_burn).total_seconds()
            if is_sat1:
                if sat2_custom:
                    launch_time = datetime.fromisoformat(sat2_custom["launch_time"])
                    r0 = [sat2_custom["init_x"], sat2_custom["init_y"], sat2_custom["init_z"]]
                    v0 = [sat2_custom["init_vx"], sat2_custom["init_vy"], sat2_custom["init_vz"]]
                    dt_sec = (tca_precise_dt - launch_time).total_seconds()
                    r_unburned_tca, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
                    err_unburned = 0
                else:
                    jd_tca, fr_tca = jday(tca_precise_dt.year, tca_precise_dt.month, tca_precise_dt.day, tca_precise_dt.hour, tca_precise_dt.minute, tca_precise_dt.second)
                    err_unburned, r_unburned_tca, _ = sat2_rec.sgp4(jd_tca, fr_tca)
            else:
                if sat1_custom:
                    launch_time = datetime.fromisoformat(sat1_custom["launch_time"])
                    r0 = [sat1_custom["init_x"], sat1_custom["init_y"], sat1_custom["init_z"]]
                    v0 = [sat1_custom["init_vx"], sat1_custom["init_vy"], sat1_custom["init_vz"]]
                    dt_sec = (tca_precise_dt - launch_time).total_seconds()
                    r_unburned_tca, _ = propagate_keplerian_rk4(r0, v0, dt_sec)
                    err_unburned = 0
                else:
                    jd_tca, fr_tca = jday(tca_precise_dt.year, tca_precise_dt.month, tca_precise_dt.day, tca_precise_dt.hour, tca_precise_dt.minute, tca_precise_dt.second)
                    err_unburned, r_unburned_tca, _ = sat1_rec.sgp4(jd_tca, fr_tca)
                    
            if err_unburned != 0 or r_unburned_tca is None:
                continue
                
            for direction in directions:
                u = dir_vectors[direction]
                for dv in dv_values:
                    dv_km_s = dv / 1000.0
                    v_b_new = [v_b[i] + dv_km_s * u[i] for i in range(3)]
                    
                    r_burned_tca, _ = propagate_keplerian_rk4(r_b, v_b_new, dt_seconds)
                    sim_dist = math.sqrt((r_burned_tca[0]-r_unburned_tca[0])**2 + (r_burned_tca[1]-r_unburned_tca[1])**2 + (r_burned_tca[2]-r_unburned_tca[2])**2)
                    
                    if sim_dist >= safety_threshold:
                        if best_burn is None or dv < best_burn["delta_v_m_s"] or (dv == best_burn["delta_v_m_s"] and sim_dist > best_burn["simulated_distance_km"]):
                            best_burn = {
                                "burn_satellite": sat_choice,
                                "burn_direction": direction,
                                "burn_hours_before_tca": lead_h,
                                "delta_v_m_s": dv,
                                "simulated_distance_km": round(sim_dist, 3),
                                "solved": True
                            }
                        break
                    else:
                        if sim_dist > best_sim_dist:
                            best_sim_dist = sim_dist
                            if best_burn is None or not best_burn.get("solved"):
                                best_burn = {
                                    "burn_satellite": sat_choice,
                                    "burn_direction": direction,
                                    "burn_hours_before_tca": lead_h,
                                    "delta_v_m_s": dv,
                                    "simulated_distance_km": round(sim_dist, 3),
                                    "solved": False
                                }
                                
    operator_email = email if email else "operator"
    if best_burn:
        log_operator_action(
            operator_email,
            "MANEUVER_SOLVE",
            f"Auto-solved conjunction {payload.sat1_name} ↔ {payload.sat2_name}. Suggested {best_burn['burn_direction']} burn on '{payload.sat1_name if best_burn['burn_satellite'] == 'sat1' else payload.sat2_name}' (Delta-V: {best_burn['delta_v_m_s']} m/s) at TCA-{best_burn['burn_hours_before_tca']}h (Solved separation: {best_burn['simulated_distance_km']} km)"
        )
        return {
            "sat1_name": payload.sat1_name,
            "sat2_name": payload.sat2_name,
            "original_distance_km": round(fine_min_dist, 3),
            **best_burn
        }
    else:
        raise HTTPException(status_code=400, detail="Could not solve maneuver optimization")


@app.get("/api/admin/users")
def get_admin_users(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Flight Director"])
    users_list = get_registered_users()
    if email not in users_list:
        users_list.append(email)
    users_with_roles = [{"email": u, "role": get_user_role(u)} for u in users_list]
    return {"ok": True, "users": users_with_roles}


@app.post("/api/admin/change-role")
def change_user_role(payload: RoleChangeRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Flight Director"])
    if payload.new_role not in ("Viewer", "Operator", "Flight Director"):
        raise HTTPException(status_code=400, detail="Invalid role specified")
    store_set(f"role:{payload.target_email.lower()}", payload.new_role)
    log_operator_action(
        email, 
        "ROLE_CHANGE", 
        f"Changed role of user '{payload.target_email}' to '{payload.new_role}'"
    )
    return {"ok": True, "target_email": payload.target_email, "new_role": payload.new_role}


@app.delete("/api/user/custom-satellites")
def clear_all_custom_satellites(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Flight Director"])
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    users = get_registered_users()
    
    if email not in users:
        users.append(email)
        
    cleared_count = 0
    for u in users:
        key = f"custom_sats:{u}"
        if url and token:
            try:
                requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=["DEL", key],
                    timeout=6
                )
            except Exception:
                pass
        if key in _memory_store:
            del _memory_store[key]
        cleared_count += 1
        
    log_operator_action(email, "DB_CLEAR_CUSTOM_SATS", f"Cleared all custom spacecraft. Affected operator accounts: {cleared_count}")
    return {"ok": True, "detail": f"Cleared custom satellites for {cleared_count} operators"}


@app.delete("/api/history/audit")
def clear_audit_logs(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Flight Director"])
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        try:
            requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["DEL", "audit:log"],
                timeout=6
            )
        except Exception:
            pass
    _memory_store["audit:log"] = []
    log_operator_action(email, "DB_CLEAR_AUDIT", "Flight Director cleared operator audit trail logs")
    return {"ok": True, "detail": "Audit logs successfully cleared"}


@app.delete("/api/history/conjunctions")
def clear_conjunction_history(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    email = verify_session_and_role(authorization, ["Flight Director"])
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        try:
            requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["DEL", "history:conjunctions"],
                timeout=6
            )
            requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=["DEL", "history:logged_set"],
                timeout=6
            )
        except Exception:
            pass
    _memory_store["history:conjunctions"] = []
    if "history:logged_set" in _memory_store:
        _memory_store["history:logged_set"] = set()
    log_operator_action(email, "DB_CLEAR_CONJUNCTIONS", "Flight Director cleared conjunction history tracking log")
    return {"ok": True, "detail": "Conjunction logs successfully cleared"}




