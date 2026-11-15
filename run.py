"""Start the app:  python run.py   (then open http://127.0.0.1:8000)"""
import os
import logging

import uvicorn

from kalshi_edge.server import create_app, load_dotenv

if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(create_app(), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")), log_level="warning")
