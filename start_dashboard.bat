@echo off
title WhatsApp Messages & Bookings Dashboard
echo ======================================================================
echo   Launching WhatsApp Groq AI Bot & Messages Dashboard...
echo ======================================================================
echo.
echo Opening browser at http://localhost:8000 ...
start http://localhost:8000
echo.
py -m uvicorn api.index:app --host 0.0.0.0 --port 8000 --reload
pause
