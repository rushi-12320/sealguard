import argparse
import os

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="SealGuard - Agentic Quality Ops Center")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    a = ap.parse_args()
    print(f"SealGuard running on http://{a.host}:{a.port}")
    uvicorn.run("sealguard.app:app", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
