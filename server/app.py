"""Stub FastAPI app for [P1.1].

This is a placeholder so `uvicorn server.app:app` can start during P1.1.
The real app — login, chat, WebSocket, setup routing — is delivered in:

  - KKallas/Imp#2  P1.2 server/auth.py
  - KKallas/Imp#3  P1.3 server/app.py (this file, rewritten)
  - KKallas/Imp#4  P1.4 ui/index.html + app.js
  - KKallas/Imp#5  P1.5 ui/renderers/markdown.js
"""

from fastapi import FastAPI

app = FastAPI(title="Imp (P1.1 stub)")


@app.get("/")
def root() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "Imp is running. The real UI lands in P1.3+ (KKallas/Imp#3).",
    }
