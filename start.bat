@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv. Run: uv sync --locked --extra browser
    popd
    exit /b 2
)
".venv\Scripts\python.exe" -X utf8 -m as_researcher.cli --interactive
set "research_exit=%errorlevel%"
popd
exit /b %research_exit%
