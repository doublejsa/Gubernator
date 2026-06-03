#!/usr/bin/env python3
"""Start Gubernator locally — http://localhost:8000"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=["*.pyc", ".venv/*", "__pycache__/*"],
        log_level="info",
    )
