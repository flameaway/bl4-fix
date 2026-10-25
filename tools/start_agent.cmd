@echo off
rem Opens the desktop agent in its own window. Close the window to stop it.
title BL4 desktop agent
cd /d "%~dp0.."
rem Set BL4_PYTHON to a python.exe if "python" isn't on the PATH of the desktop session.
if not defined BL4_PYTHON set BL4_PYTHON=python
"%BL4_PYTHON%" tools\desktop_agent.py serve
