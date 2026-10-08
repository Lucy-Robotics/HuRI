"""Run the playground: ``python -m src.playground`` (see README, "Playground")."""

import logging
import os

import uvicorn

from .gateway import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(),
        host=os.environ.get("PLAYGROUND_HOST", "127.0.0.1"),
        port=int(os.environ.get("PLAYGROUND_PORT", "8080")),
    )
