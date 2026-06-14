# OrbitWatch

**Agentic Space Debris Intelligence System**  
FAR AWAY 2026 Hackathon Submission | Theme: Space & Aerospace

OrbitWatch monitors satellite conjunction risk from Two-Line Element (TLE) orbital data, propagates satellite positions with SGP4, detects close approaches, and produces plain-English alerts with avoidance maneuver suggestions.

## Live Submission

- Live dashboard: <https://orbitwatch-five.vercel.app>
- Live API stats: <https://orbitwatch-five.vercel.app/api/stats>
- Live satellite positions: <https://orbitwatch-five.vercel.app/api/satellites>
- Live conjunction scan: <https://orbitwatch-five.vercel.app/api/conjunctions?threshold_km=10000>
- Live AI briefing endpoint: <https://orbitwatch-five.vercel.app/api/agent-briefing>
- GitHub repository: <https://github.com/AbhishekKharat04/orbitwatch>

## Why It Matters

Earth orbit contains tens of thousands of tracked objects. A single high-energy collision can create more debris, increasing the risk of a cascading Kessler Syndrome event and threatening GPS, communications, weather, defense, internet, and Earth-observation infrastructure.

OrbitWatch turns space situational awareness into an accessible software MVP for small satellite teams, student missions, and rapid-response operators.

## Demo

- Hosted dashboard: <https://orbitwatch-five.vercel.app>
- Vercel FastAPI backend: `frontend/api/index.py`
- Local FastAPI backend: `backend/main.py`
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
- Hosted dashboard with live Vercel Python serverless API integration.
- Real-time CelesTrak GP/TLE ingestion for the stations catalog, with fallback demo data if the upstream feed is unavailable.
- OpenAI Responses API operator briefings when `OPENAI_API_KEY` is configured.
- Email-code auth workflow, watchlist storage hooks, and alert email delivery when Upstash Redis and Resend keys are configured.
- 3D TEME-frame orbit visualization using live SGP4 x/y/z coordinates.
- FastAPI documentation at `/docs`.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Backend | Python, FastAPI |
| Hosted API | Vercel Python Serverless Functions |
| Orbital Mechanics | sgp4 |
| Live Orbital Data | CelesTrak GP TLE endpoint |
| AI Briefing | OpenAI Responses API |
| Auth / Database | Email code flow + Upstash Redis REST |
| Alert Delivery | Resend email API |
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
| `GET /api/satellites` | Current SGP4-propagated CelesTrak satellite positions |
| `GET /api/conjunctions?threshold_km=50` | Detected conjunction events |
| `GET /api/agent-briefing` | AI/operator briefing; uses OpenAI when configured |
| `POST /api/auth/request-code` | Request email login code |
| `POST /api/auth/verify-code` | Verify login code and receive bearer token |
| `GET/POST /api/user/watchlist` | Read or update authenticated watchlist |
| `POST /api/alerts/test` | Generate and optionally email a test alert |

The hosted API uses CelesTrak GP data by default. If CelesTrak is unreachable, OrbitWatch falls back to a small demo TLE catalog so the dashboard remains available during upstream outages.

## Production Integrations

Add these environment variables in Vercel to activate the full production workflow:

```env
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
APP_SECRET=replace-with-a-long-random-secret
UPSTASH_REDIS_REST_URL=...
UPSTASH_REDIS_REST_TOKEN=...
RESEND_API_KEY=...
ALERT_FROM_EMAIL=OrbitWatch <verified-sender@example.com>
```

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
