@echo off
rem Suki Post Studio — 一鍵啟動（瀏覽器開 http://127.0.0.1:8765）
rem 提示：要用「發布」功能嘅話，請先啟動 xiaohongshu-mcp 服務：
rem   cd /d D:\Projects\Suki\tools\xiaohongshu-mcp
rem   xiaohongshu-mcp-fixed-windows-amd64.exe -headless=false
cd /d %~dp0
start "" http://127.0.0.1:8765
python server.py
