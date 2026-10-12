"""End-to-end smoke test of a running HuRI server, through the client SDK.

    HURI_MODULES=tag,qag  (on the server)
    python deploy/smoke_session.py ws://127.0.0.1:8000/session

Sends a transcript through TAG -> QAG and expects the aggregated question back;
needs no model, so it runs against any image. Exits non-zero on failure.
"""

import asyncio
import sys

from huri_events import RAGQuestion, Transcript

from huri_core.core.client import HuRIClient


async def main(url: str) -> int:
    got: list = []

    async def on_question(question: RAGQuestion) -> None:
        got.append(question)

    client = HuRIClient(
        url,
        modules={
            "tag": {"name": "tag"},
            "qag": {"name": "qag", "args": {"use_emotion": False}},
        },
        sends=["huri.transcript"],
        user_id="smoke-test",
    )
    client.on("huri.question", RAGQuestion, on_question)
    async with client:
        await client.send("huri.transcript", Transcript("hello robot", True))
        for _ in range(300):
            if got:
                break
            await asyncio.sleep(0.1)
    expected = RAGQuestion(Transcript("hello robot", True), None)
    print(f"received: {got}")
    return 0 if got == [expected] else 1


if __name__ == "__main__":
    sys.exit(
        asyncio.run(
            main(sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000/session")
        )
    )
