@echo off
setlocal EnableExtensions
chcp 65001 >nul
title YouTube Downloader — встановлення
cd /d "%~dp0"

echo ==================================================
echo   YouTube Downloader — встановлення
echo ==================================================
echo.

rem ---------- Крок 1. Python 3.10+ ----------
echo [1/8] Перевірка Python...
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :python_ok
echo Python 3.10 або новіший не знайдено.
where winget >nul 2>&1
if errorlevel 1 goto :python_manual
choice /c YN /m "Встановити Python 3.12 автоматично через winget"
if errorlevel 2 goto :python_manual
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Python встановлено. Закрийте це вікно та запустіть install.bat ще раз.
goto :end_wait
:python_manual
echo Завантажте Python з https://www.python.org/downloads/ і під час встановлення
echo поставте галочку "Add python.exe to PATH". Потім запустіть install.bat ще раз.
start "" "https://www.python.org/downloads/"
goto :end_wait
:python_ok
for /f "delims=" %%v in ('python --version 2^>^&1') do echo Знайдено: %%v

rem ---------- Крок 2. Залежності Python ----------
echo.
echo [2/8] Встановлення залежностей Python...
python -m pip install -U pip
if errorlevel 1 goto :fail_pip
python -m pip install -U "yt-dlp[default]" ytmusicapi pystray pillow
if errorlevel 1 goto :fail_pip

rem ---------- Крок 3. FFmpeg ----------
echo.
echo [3/8] Перевірка FFmpeg...
set "NEED_FFMPEG="
ffmpeg -version >nul 2>&1
if errorlevel 1 set "NEED_FFMPEG=1"
ffprobe -version >nul 2>&1
if errorlevel 1 set "NEED_FFMPEG=1"
if not defined NEED_FFMPEG goto :ffmpeg_ok
echo FFmpeg не знайдено.
where winget >nul 2>&1
if errorlevel 1 goto :ffmpeg_manual
choice /c YN /m "Встановити FFmpeg автоматично через winget"
if errorlevel 2 goto :ffmpeg_manual
winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
echo.
echo FFmpeg встановлено. Закрийте це вікно та запустіть install.bat ще раз,
echo щоб Windows оновила PATH.
goto :end_wait
:ffmpeg_manual
echo Встановіть FFmpeg вручну: https://www.gyan.dev/ffmpeg/builds/
echo Додайте папку bin до PATH і запустіть install.bat ще раз.
goto :end_wait
:ffmpeg_ok
echo FFmpeg знайдено.

rem ---------- Крок 4. Deno ----------
echo.
echo [4/8] Перевірка Deno...
deno --version >nul 2>&1
if not errorlevel 1 goto :deno_ok
echo Deno не знайдено. Він потрібен yt-dlp для роботи з YouTube.
where winget >nul 2>&1
if errorlevel 1 goto :deno_manual
choice /c YN /m "Встановити Deno автоматично через winget"
if errorlevel 2 goto :deno_manual
winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements
echo.
echo Deno встановлено. Закрийте це вікно та запустіть install.bat ще раз.
goto :end_wait
:deno_manual
echo Встановіть Deno вручну: https://deno.com/ і запустіть install.bat ще раз.
goto :end_wait
:deno_ok
echo Deno знайдено.

rem ---------- Кроки 5-6. Папки та config.json ----------
echo.
echo [5-6/8] Робочі папки та config.json...
if exist "config.json" goto :config_exists
if not exist "config.example.json" goto :fail_example
copy /y "config.example.json" "config.json" >nul
echo Створено config.json зі зразка.
goto :config_init
:config_exists
echo config.json вже існує — залишаю без змін.
:config_init
rem load_config генерує випадковий токен лише якщо його ще немає
python -c "import youtube_downloader as y; c = y.load_config(); y.ensure_dirs(c)"
if errorlevel 1 goto :fail_config
echo Папки "завантажені відео" та "завантажені аудіо" готові.

rem ---------- Крок 7. Автозапуск ----------
echo.
echo [7/8] Автозапуск при вході в Windows...
for /f "delims=" %%p in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"') do set "YTDL_PYW=%%p"
set "YTDL_SCRIPT=%~dp0youtube_downloader.py"
set "YTDL_DIR=%~dp0"
set "YTDL_LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\YouTube Downloader.lnk"
if not exist "%YTDL_PYW%" goto :fail_pythonw
rem Ярлик з тією самою назвою перезаписується, тому дублів не буде
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:YTDL_LNK); $s.TargetPath=$env:YTDL_PYW; $s.Arguments=[char]34+$env:YTDL_SCRIPT+[char]34; $s.WorkingDirectory=$env:YTDL_DIR.TrimEnd('\'); $s.Description='YouTube Downloader'; $s.Save()"
if errorlevel 1 goto :fail_shortcut
if not exist "%YTDL_LNK%" goto :fail_shortcut
echo Ярлик автозапуску створено.

rem Запускаємо сервер зараз, якщо він ще не працює
python -c "import json,urllib.request as u; p=json.load(open('config.json',encoding='utf-8-sig'))['server']['port']; u.urlopen('http://127.0.0.1:%%d/api/health' %% p, timeout=2)" >nul 2>&1
if not errorlevel 1 goto :server_running
start "" "%YTDL_PYW%" "%YTDL_SCRIPT%"
echo Сервер запущено: його вікно з токеном відкриється за кілька секунд.
goto :final
:server_running
echo Сервер вже працює.

rem ---------- Крок 8. Фінальне повідомлення ----------
:final
echo.
echo [8/8] Готово! Далі — встановлення розширення в Chrome:
echo.
echo   1. Зараз відкриється папка з розширенням. Не закривайте її.
echo   2. Відкрийте Chrome - chrome://extensions - увімкніть "Режим розробника".
echo   3. Натисніть "Завантажити розпаковане" і виберіть папку extension\.
echo   4. Скопіюйте ID розширення (він у картці розширення) і вставте його
echo      в config.json у поле allowed_extension_ids, потім перезапустіть сервер
echo      (вікно сервера - "Зупинити сервер" - "Запустити").
echo   5. Відкрийте налаштування розширення (іконка в панелі Chrome) і вставте
echo      токен із вікна сервера.
echo.
start "" explorer "%~dp0extension"
if exist "README.md" start "" "%~dp0README.md"
goto :end_wait

:fail_pip
echo.
echo ПОМИЛКА: не вдалося встановити залежності Python. Перевірте інтернет і
echo запустіть install.bat ще раз.
goto :end_wait
:fail_example
echo.
echo ПОМИЛКА: не знайдено config.example.json поруч з install.bat.
goto :end_wait
:fail_config
echo.
echo ПОМИЛКА: не вдалося створити config.json. Дивіться повідомлення вище.
goto :end_wait
:fail_pythonw
echo.
echo ПОМИЛКА: не знайдено pythonw.exe: %YTDL_PYW%
goto :end_wait
:fail_shortcut
echo.
echo ПОМИЛКА: не вдалося створити ярлик автозапуску.
goto :end_wait

:end_wait
echo.
pause
endlocal
