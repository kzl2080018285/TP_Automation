@echo off
cd /d "%~dp0"
rem Launch the Qt GUI with a Python that has PySide6 installed.
py -3.14 adbcase_gui.py
if errorlevel 1 python adbcase_gui.py
