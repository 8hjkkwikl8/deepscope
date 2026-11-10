@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo 未找到 Python。请先安装 Python 3.11 或更高版本，并勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

%PY% -c "import fastapi, uvicorn, httpx, bs4, dotenv" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装依赖（使用阿里云镜像）...
  %PY% -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
  if errorlevel 1 (
    echo.
    echo 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo.
echo 启动 DeepScope，浏览器打开 http://127.0.0.1:8765
echo.
%PY% run.py
pause
