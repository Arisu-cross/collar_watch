# main.py — entrypoint shim. The app itself lives in server/app.py.
#
# Why this file exists (2026-08-21): this service was originally deployed with
# a `docker` plan that carried its own CMD. Re-deploying the source without a
# Dockerfile makes Zeabur pick the `python` plan instead, and that plan starts
# `python main.py` — so the build succeeds, the container boots, and dies with
# `can't open file '/app/main.py'`. The fix is to give it the entrypoint it
# looks for rather than to fight the plan detection.
#
# server/app.py resolves its own paths from __file__, so running it through
# runpy from the project root behaves exactly like `python server/app.py`.
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "server" / "app.py"),
               run_name="__main__")
