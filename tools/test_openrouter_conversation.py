"""LIVE end-to-end conversation test through LUFY's real AI brain (OpenRouter by default).

    python tools/test_openrouter_conversation.py [--model provider/model]

Runs a 3-turn conversation: streaming, conversation memory (turn 2 must recall turn 1), Hinglish.
Uses a throwaway database: your saved LUFY history/memories are not touched and nothing is stored.
Cost: three short replies (a fraction of a cent). The API key is never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.ai_brain import AIBrain  # noqa: E402
from core.errors import LufyError, classify, redact  # noqa: E402
from core.memory import Memory  # noqa: E402
from database.database import Database  # noqa: E402

TURNS = [
    ("Hi LUFY! My favourite colour is teal. Please remember that for this conversation.", None),
    ("What is my favourite colour? Answer in one short sentence.", "teal"),
    ("Mujhe ek chhota sa Hinglish joke sunao.", None),
]


async def run(model: str | None) -> int:
    tmp = tempfile.mkdtemp()
    mem = Memory(Database(Path(tmp) / "conv.db"))
    mem.set("llm_provider", "openai")
    if model:
        mem.set("llm_model", model)
    brain = AIBrain(mem)
    brain.on_retry = lambda err, d, n, t: print(f"   (temporary {err.kind}: retry {n}/{t} in {d:.0f}s)")
    print(f"Endpoint: {config.ENV.openai_base_url}\nAI:       {brain.describe()}\n")
    failures = 0
    for n, (prompt, expect) in enumerate(TURNS, 1):
        print(f"[{n}] YOU : {prompt}")
        mem.add_message("user", prompt)
        t0, first, chunks, text = time.time(), None, 0, ""
        try:
            print("    LUFY: ", end="", flush=True)
            async for delta in brain.reply_stream(prompt):
                first = first or time.time() - t0
                chunks += 1
                text += delta
                print(delta, end="", flush=True)
            print()
        except Exception as e:
            err = classify(e, "AI", vendor=config.vendor())
            print(f"\n[FAIL] {redact(err.user_message)}")
            return 1
        mem.add_message("assistant", text)
        print(f"    ({chunks} chunks, first token {first:.2f}s, total {time.time() - t0:.2f}s)")
        if not text.strip():
            print("[FAIL] empty reply"); failures += 1
        if expect and expect not in text.lower():
            print(f"[FAIL] conversation history NOT preserved: reply should mention '{expect}'"); failures += 1
        if chunks < 2:
            print("[WARN] reply arrived as a single chunk - streaming may not be working (short replies can legitimately be 1-2 chunks)")
    print("\n" + ("[ OK ] Real conversation works: streaming, history and Hinglish." if not failures else f"[FAIL] {failures} check(s) failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="override the model for this run, e.g. openai/gpt-4o-mini")
    sys.exit(asyncio.run(run(ap.parse_args().model)))
