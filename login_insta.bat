@echo off
title Instagram Login - Clipwa
cd /d "C:\Users\Abhinav\.gemini\antigravity\scratch\clipwa"
echo =======================================================
echo  Launching Instagram Persistent Login Browser...
echo =======================================================
"C:\Users\Abhinav\AppData\Local\Programs\Python\Python314\python.exe" -m clipwa.cli login-insta --timeout 300
echo.
echo Login process finished.
pause
