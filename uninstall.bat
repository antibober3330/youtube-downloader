@echo off
setlocal EnableExtensions
chcp 65001 >nul
title YouTube Downloader — видалення
cd /d "%~dp0"

echo ==================================================
echo   YouTube Downloader — видалення
echo ==================================================
echo.

rem ---------- 1. Зупинка сервера ----------
echo [1/4] Зупиняю сервер...
rem Спершу просимо сервер завершитися коректно (POST /api/shutdown)
python -c "import json,urllib.request as u; p=json.load(open('config.json',encoding='utf-8-sig'))['server']; u.urlopen(u.Request('http://127.0.0.1:%%d/api/shutdown' %% p['port'], b'{}', {'Authorization': 'Bearer ' + p['token'], 'Content-Type': 'application/json'}, method='POST'), timeout=5)" >nul 2>&1
if errorlevel 1 (echo Сервер не відповів — перевіряю процеси.) else (echo Сервер отримав команду завершення.)
timeout /t 3 /nobreak >nul
rem Якщо процес лишився — завершуємо лише python, запущений з youtube_downloader.py
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -like '*youtube_downloader.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

rem ---------- 2. Ярлик автозапуску ----------
echo.
echo [2/4] Видаляю ярлик автозапуску...
set "YTDL_LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\YouTube Downloader.lnk"
if exist "%YTDL_LNK%" (
    del /f /q "%YTDL_LNK%"
    echo Ярлик видалено.
) else (
    echo Ярлика автозапуску немає.
)

rem ---------- 3. Що залишається на диску ----------
echo.
echo [3/4] Ваші файли НЕ видалено. Вони лежать тут:
echo   Налаштування:     %~dp0config.json
echo   Архів і звіти:    %~dp0downloaded.txt, %~dp0failed.txt, %~dp0log.txt
echo   Відео:            %~dp0завантажені відео
echo   Аудіо:            %~dp0завантажені аудіо
echo Видалити їх можна вручну, коли вони більше не потрібні.

rem ---------- 4. Залежності Python (необов'язково) ----------
echo.
echo [4/4] Залежності Python (yt-dlp, ytmusicapi, pystray, pillow).
choice /c YN /n /m "Видалити їх? Інші програми на цьому комп'ютері можуть їх використовувати [Y/N]: "
if errorlevel 2 goto :skip_pip
python -m pip uninstall -y yt-dlp ytmusicapi pystray pillow
:skip_pip

echo.
echo Видалення завершено. Папку з програмою можна видалити вручну,
echo розширення — прибрати на сторінці chrome://extensions.
echo.
pause
endlocal
