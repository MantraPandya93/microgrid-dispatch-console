"""Create ./build : the app code + its Python dependencies, ready for `catalyst deploy` (AppSail).

    python tools/make_build.py

Catalyst's managed Python runtime expects the dependencies to sit next to app.py, so this copies
app/ into build/ (without tests) and runs `pip install -r app/requirements.txt -t build`.

AppSail runs Linux with Python 3.13, so pip is told to fetch Linux (manylinux) wheels for 3.13
even when this script runs on Windows. Some dependencies (pydantic-core, used by the Anthropic SDK)
are compiled and would not load on the server otherwise.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app")
DST = os.path.join(ROOT, "build")

if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r",
                       os.path.join(SRC, "requirements.txt"), "-t", DST,
                       "--platform", "manylinux2014_x86_64", "--python-version", "3.13",
                       "--implementation", "cp", "--only-binary=:all:", "--upgrade"])
print("Build ready:", DST)
print("Startup command for AppSail: python3 -u app.py")
