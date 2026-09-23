import argparse
import sys
from pathlib import Path

# Make imports of main.py / ingest.py work no matter where we're launched from.
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the RAG chatbot server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", default=8000, type=int, help="Port to serve on")
    parser.add_argument("--reload", action="store_true",
                        help="Auto-restart on code changes (dev mode)")
    args = parser.parse_args()

    import uvicorn

    print(f"Starting server on http://{args.host}:{args.port}  (UI: /  Docs: /docs)")
    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
