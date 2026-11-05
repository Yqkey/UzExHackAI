"""Веб-сервис: python -m uvicorn web:app --host 0.0.0.0 --port 8000"""
import asyncio, pathlib
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import agent

app = FastAPI(title="Birja bitimlari agenti")

class Q(BaseModel):
    question: str

@app.get("/")
def index(): return FileResponse(pathlib.Path(__file__).parent / "static" / "index.html")

@app.get("/health")
def health(): return {"ok": True}

@app.post("/api/ask")
async def ask(q: Q):
    text = q.question.strip()
    if not text: return JSONResponse({"error": "empty"}, status_code=400)
    try:
        r = await asyncio.to_thread(agent.ask, text[:2000])
    except Exception as e:
        return JSONResponse({"answer": "Сервис временно недоступен (LLM). Xizmat vaqtincha ishlamayapti.", "error": str(e)[:200], "sql": [], "elapsed_sec": 0, "grounded": False})
    r.pop("trace", None)
    return r
