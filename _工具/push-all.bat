@echo off
REM ---------------------------------------------------------------
REM  Push this project to BOTH remotes in one go.
REM  (GitHub = origin/main,  Gitee = gitee/master)
REM
REM  Just double-click this file, or run it from a terminal.
REM  Why the odd "main:master" on the Gitee line: our local branch
REM  is "main", but the Gitee repo's default branch is "master".
REM ---------------------------------------------------------------
cd /d "%~dp0.."

echo.
echo === [1/2] Pushing to GitHub (origin) ===
git push origin main
if errorlevel 1 echo   ^>^> GitHub push FAILED, see message above.

echo.
echo === [2/2] Pushing to Gitee (gitee) ===
git push gitee main:master
if errorlevel 1 echo   ^>^> Gitee push FAILED, see message above.

echo.
echo === Status ===
git status -sb

echo.
echo Done. Press any key to close.
pause >nul
