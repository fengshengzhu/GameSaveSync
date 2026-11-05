@echo off
chcp 65001 >nul
echo ================= GameSaveSync 打包脚本 =================
echo 依赖检查：PyInstaller（需 Python3.9+）
python -m pip install -U pyinstaller || goto :fail
echo.
echo 开始把 run.py 打包为 GameSaveSync.exe
:: --onefile 单文件；--noconsole 隐藏黑窗（tk GUI 程序）
python -m PyInstaller -F -w -n GameSaveSync run.py || goto :fail
echo.
echo ======================================
echo 完成！exe 在 dist 文件夹内
pause
exit /b 0

:fail
echo.
echo 打包失败，请查看上方红色错误信息
pause
exit /b 1
