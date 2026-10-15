# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer, running locally or available through MongoDB Atlas
- PowerShell on Windows (commands below); equivalent commands work in other shells with small syntax changes

## Step-by-step setup (Windows PowerShell)

1. Extract the submission ZIP and open PowerShell in the extracted project folder, the one containing `requirements.txt` and the `app` folder.

2. Start MongoDB. For a local Docker instance, run this once:

   ```powershell
   docker run --name attendance-mongo -p 27017:27017 -d mongo:7
   ```

   If you already created the container, start it with `docker start attendance-mongo`. You can also use a MongoDB Atlas connection string instead.

3. Create and activate a virtual environment:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If activation is blocked by the PowerShell execution policy, skip activation and use `.\.venv\Scripts\python.exe` in place of `python` in the following commands.

4. Upgrade pip and install or upgrade all app dependencies from the requirements file:

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install --upgrade -r requirements.txt
   ```

   The version ranges allow pip to select the newest compatible release within each supported major version. `--upgrade` updates packages already installed in this environment.

5. Configure the MongoDB connection. For the Docker setup above:

   ```powershell
   $env:MONGO_URI = "mongodb://localhost:27017"
   $env:MONGO_DB = "attendance_db"
   ```

   For Atlas, set `MONGO_URI` to your Atlas connection string and `MONGO_DB` to the database name. Keep credentials private; do not commit a real connection string.

6. Start the API:

   ```powershell
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

   The application creates its MongoDB indexes on startup. Leave this terminal running while using the API.

7. Verify it is running from another PowerShell window:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8000/health
   ```

   The interactive API documentation is at `http://127.0.0.1:8000/docs`; OpenAPI JSON is at `http://127.0.0.1:8000/openapi.json`. Stop the server with Ctrl+C.

## Using the API

The API starts with an empty database unless you add data. Create employees through the employee endpoints in `/docs`, then use the punch and analytics endpoints. If using the original assignment kit's sample seed script and sample data, run that script against a disposable database before starting the API; it clears and reloads the `employees` and `attendance_logs` collections in the selected database.

## Implementation and validation

Employee CRUD, attendance punches and corrections, attendance listing, the analytics endpoints, index creation, and the explain endpoint are implemented in `app/main.py`. Design decisions and the starter-code review are documented in `DECISIONS.md` and `REVIEW.md`.

Validated locally with MongoDB 7.0.20 against the provided sample data and additional cases for shift boundaries, missing-log trend dates, ranking ties, audit history, explain plans, and concurrent punches/corrections.
