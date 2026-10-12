# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
# create a .env file (not committed) or set real environment variables:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db
uvicorn app.main:app --port 8000
```
Interactive docs: http://localhost:8000/docs. Indexes are created automatically at startup.
Needs MongoDB 6.0+ (the analytics use `$setWindowFields`, `$dateAdd`, `$lookup` with a sub-pipeline).

## Status
All endpoints in `openapi.yaml` are implemented. Nothing is knowingly missing.

## Choices where the contract left room
- Trend range limit: at most 92 calendar days *counted inclusively* (93 rows -> 422).
- `PATCH` re-validates the punch-in/punch-out gap only when a punch time is supplied in that request.
- Explain endpoints do not return 404 for unknown employees/departments; they just explain the query.
- Attendance/leaderboard/summary read the *stored* derived values, as the contract asks.
