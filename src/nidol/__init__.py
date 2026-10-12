import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "nidol.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
