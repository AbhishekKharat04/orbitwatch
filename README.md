# OrbitWatch

**Agentic Space Debris Intelligence System**  
FAR AWAY 2026 Hackathon Submission | Theme: Space & Aerospace

OrbitWatch monitors satellite conjunction risk from Two-Line Element (TLE) orbital data, propagates satellite positions with SGP4, detects close approaches, and produces plain-English alerts with avoidance maneuver suggestions.

## Why It Matters

Earth orbit contains tens of thousands of tracked objects. A single high-energy collision can create more debris, increasing the risk of a cascading Kessler Syndrome event and threatening GPS, communications, weather, defense, internet, and Earth-observation infrastructure.

OrbitWatch turns space situational awareness into an accessible software MVP for small satellite teams, student missions, and rapid-response operators.

## Demo

- Frontend dashboard: `frontend/index.html`
- FastAPI backend: `backend/main.py`
- Submission deck: `docs/OrbitWatch_FAR_AWAY_2026.pptx`

## Architecture

```text
TLE catalog -> SGP4 propagation -> proximity engine -> risk scoring
                                                -> natural-language alerts
                                                -> dashboard/API
```

## Features

- SGP4 orbital propagation using the same class of TLE-based math used in operational tracking workflows.
- Conjunction detection with configurable distance thresholds.
- Risk scoring: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`.
- Actionable alert text with maneuver guidance.
- Offline-first dashboard with demo data and live API integration when the backend is running.
- FastAPI documentation at `/docs`.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python, FastAPI |
| Orbital Mechanics | sgp4 |
| Frontend | HTML, CSS, JavaScript |
| API Docs | OpenAPI / Swagger UI |
| Submission Material | PowerPoint deck |

## Quick Start

```bash
git clone https://github.com/AbhishekKharat04/orbitwatch.git
cd orbitwatch

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

uvicorn backend.main:app --reload --port 8000
```

Then open:

- API: <http://localhost:8000>
- API docs: <http://localhost:8000/docs>
- Dashboard: `frontend/index.html`

You can also serve the dashboard locally:

```bash
python -m http.server 3000 --directory frontend
```

Then open <http://localhost:3000>.

## API Endpoints

| Endpoint | Description |
| --- | --- |
| `GET /` | Service status |
| `GET /api/stats` | OrbitWatch summary metrics |
| `GET /api/satellites` | Current propagated demo satellite positions |
| `GET /api/conjunctions?threshold_km=50` | Detected conjunction events |

## Submission Checklist

- GitHub repository link
- Project deck: `docs/OrbitWatch_FAR_AWAY_2026.pptx`
- Working MVP with dashboard and API
- Clear README with setup, architecture, and impact

## Future Scope

- Live CelesTrak catalog ingestion.
- Space-Track authenticated catalog support.
- LLM-generated operator briefings.
- Email/SMS alert routing.
- Historical risk timeline and operator audit log.

## Team

HallucinateThis  
Built by Abhishek Rajesh Kharat for FAR AWAY 2026.
