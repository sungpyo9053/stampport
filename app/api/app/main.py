from fastapi import FastAPI

app = FastAPI(title="Stampport API")


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "stampport"
    }
