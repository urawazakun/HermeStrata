@echo off
setlocal
rem Hermes on the local Qwen3.8-Flash-Next (Strata IQ3_S, 262K) at 127.0.0.1:18100. Starts the server if it is not up.
set "HERMES_HOME=%~dp0hermes-home"
set "HERMES_EXE=%HERMES_HOME%\bin\hermes.exe"
rem git snapshot goes to the volatile tail so the local engine can reuse the whole fixed prefix across sessions
set "HERMES_WORKSPACE_LATE=1"
rem the title request waits until the turn has answered (one engine slot: it would delay the turn and evict the cache)
set "HERMES_TITLE_AFTER_TURN=1"
powershell -NoProfile -Command "try { (Invoke-WebRequest http://127.0.0.1:18100/health -TimeoutSec 3 -UseBasicParsing) | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [hermes] starting local Strata IQ3_S 262K server ...
  pwsh -NoProfile -File %HERMESTRATA_ROOT%\deploy-strata\start-strata-server.ps1 -Quant iq3_s -Ctx 262144 -Cache 9999 -Vision cpu -Force
)
"%HERMES_EXE%" -p flashnext chat %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
