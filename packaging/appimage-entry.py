"""PyInstaller entry point used by the AppImage build."""

from mwb_linux.__main__ import main

raise SystemExit(main())
