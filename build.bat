@echo off
REM 打包成单文件 exe,产物在 dist\Tokens-Scanner.exe
cd /d "%~dp0"

REM ui\data.json 只是浏览器调试用的缓存,绝不能打进正式包:
REM 否则桥接一旦失败,程序会拿构建那一刻的旧数据冒充当前数据。
if exist "ui\data.json" del /q "ui\data.json"

python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --windowed ^
  --name "Tokens-Scanner" ^
  --add-data "ui;ui" ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import clr_loader ^
  main.py

echo.
echo 完成,产物: dist\Tokens-Scanner.exe
pause
