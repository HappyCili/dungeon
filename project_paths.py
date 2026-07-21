"""Filesystem roots for the split UI and native-app workspace."""

from pathlib import Path


UI_APP_ROOT = Path(__file__).resolve().parent
NATIVE_APP_ROOT = UI_APP_ROOT.parent / "native_app"
