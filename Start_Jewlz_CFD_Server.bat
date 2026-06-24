@echo off
title Jewlz CFD Server Launcher

echo Starting Jewlz CFD backend system...

REM Move to CFD app folder
cd /d "C:\Users\JOEL\Desktop\Jewlz tech\Jewlz Fluid Dynamic force Simulator\CFD App"

REM Set backend API key
set JEWLZ_CFD_API_KEY=JewlzCFD2026SecureKey

REM Start Docker Desktop if not already open
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"

echo Waiting for Docker Desktop...
timeout /t 25

REM Start OpenFOAM container
docker start jewlz-openfoam

REM Start FastAPI backend
start "Jewlz CFD Backend" cmd /k "cd /d C:\Users\JOEL\Desktop\Jewlz tech\Jewlz Fluid Dynamic force Simulator\CFD App && set JEWLZ_CFD_API_KEY=JewlzCFD2026SecureKey && python -m uvicorn backend_api:app --host 0.0.0.0 --port 8000"

echo Waiting for backend...
timeout /t 8

REM Start Cloudflare quick tunnel
start "Jewlz CFD Tunnel" cmd /k "cloudflared tunnel --url http://localhost:8000 --config NUL"

echo.
echo Jewlz CFD launch sequence started.
echo Keep the backend and tunnel windows open.
pause
