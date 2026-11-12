import time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from core.assistant import Assistant
from database.database import Database


@pytest.fixture
def a(tmp_path):
    QApplication.instance() or QApplication([])
    asst = Assistant(Database(tmp_path / "a.db"))
    asst.said, asst.chat, asst.errors, asst.stopped = [], [], [], []
    asst.speaker.say = lambda t: asst.said.append(t)
    asst.speaker.stop = lambda: asst.stopped.append(1)
    asst.chat_message.connect(lambda r, t: asst.chat.append((r, t)), Qt.DirectConnection)
    asst.error.connect(asst.errors.append, Qt.DirectConnection)
    yield asst
    asst.shutdown()


def feed(a, text, seg=None):
    a.loop.call_soon_threadsafe(a._on_transcript, text, {"segment_id": seg or text})


def wait(cond, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_wake_word_required_and_command_runs(a):
    feed(a, "what's the time")                         # no wake word -> ignored
    time.sleep(0.2)
    assert not a.chat
    feed(a, "Hey Lufy what's the time")
    assert wait(lambda: a.said)
    assert a.chat[0] == ("user", "what's the time") and a.said[0].startswith("It's")


def test_lufy_alone_without_hey_triggers(a):
    feed(a, "Loofy what is the date")
    assert wait(lambda: a.said) and a.said[0].startswith("Today is")


def test_duplicate_segment_and_text_processed_once(a):
    feed(a, "Hey Lufy battery", seg="x1"); feed(a, "Hey Lufy battery", seg="x1"); feed(a, "Hey Lufy battery", seg="x2")
    assert wait(lambda: a.said)
    time.sleep(0.3)
    assert len(a.said) == 1


def test_wake_word_alone_opens_followup_window(a):
    feed(a, "Hey Lufy")
    assert wait(lambda: a.said == ["Yes?"])
    feed(a, "what is the date")                         # no wake word needed within the window
    assert wait(lambda: len(a.said) == 2) and a.said[1].startswith("Today is")


def test_echo_of_own_speech_is_ignored(a):
    a.speaker._spoken_recent = "Battery is at 82 percent and charging."
    a._last_speak_end = time.time()
    feed(a, "hey lufy battery is at 82 percent and charging")
    time.sleep(0.3)
    assert not a.chat


def test_confirmation_flow_needs_no_wake_word(a):
    a.memory.add_memory("likes jazz")
    feed(a, "Hey Lufy forget jazz")
    assert wait(lambda: a.said and "Delete" in a.said[-1])
    assert a.memory.list_memories()                    # still there
    feed(a, "yes")
    assert wait(lambda: not a.memory.list_memories())


def test_llm_reply_streams_into_sentences_and_is_saved(a):
    async def fake(user_text):
        for piece in ["Recursion is when a function ", "calls itself. ", "It needs a base case. ", "Try factorial"]:
            yield piece
    a.brain.reply_stream = fake
    feed(a, "Hey Lufy explain recursion")
    assert wait(lambda: len(a.said) >= 3)
    assert a.said == ["Recursion is when a function calls itself.", "It needs a base case.", "Try factorial"]
    assert wait(lambda: a.memory.recent_messages()[-1]["role"] == "assistant")
    assert a.memory.recent_messages()[-1]["content"].endswith("Try factorial")


def test_llm_failure_is_reported_not_crashing(a):
    async def boom(user_text):
        raise RuntimeError("kaboom")
        yield ""
    a.brain.reply_stream = boom
    feed(a, "Hey Lufy tell me a story")
    assert wait(lambda: a.errors)
    assert a.said and "couldn't get an answer" in a.said[-1]


def test_missing_llm_key_message(a):
    feed(a, "Hey Lufy tell me a story")
    assert wait(lambda: a.errors)
    assert "API key" in a.errors[0]


def test_new_utterance_interrupts_current_speech(a):
    feed(a, "Hey Lufy battery")
    assert wait(lambda: a.said)
    assert a.stopped                                    # _begin_turn always stops current speech first


def test_push_to_talk_mode_skips_wake_word_only_when_active(a):
    a.memory.set("mode", "push_to_talk")
    feed(a, "what is the date")
    time.sleep(0.2)
    assert not a.chat                                   # not armed
    a._ptt_until = time.time() + 5
    feed(a, "what is the date today")
    assert wait(lambda: a.said)


def test_gnani_key_never_logged_in_headers_repr():
    from speech.stt import GnaniSTTClient
    c = GnaniSTTClient(api_key="SECRET123")
    assert "SECRET123" not in repr(c)


def test_identical_request_is_sent_to_llm_only_once(a):
    calls = []

    async def slow(user_text):
        import asyncio
        calls.append(user_text)
        await asyncio.sleep(0.4)
        yield "Fine."
    a.brain.reply_stream = slow
    for _ in range(3):
        a.submit_text("explain recursion")                  # double/triple Enter
    assert wait(lambda: a.said)
    time.sleep(0.3)
    assert calls == ["explain recursion"]


def test_billing_error_shows_real_cause_and_speaks_specific_message(a):
    from core.errors import BillingError

    async def quota(user_text):
        raise BillingError("AI (openai): the account is out of quota/credit (HTTP 429, insufficient_quota). Not retrying.",
                           spoken="My AI provider says the account is out of credit. Please check the billing page.")
        yield ""
    a.brain.reply_stream = quota
    feed(a, "Hey Lufy tell me a story")
    assert wait(lambda: a.errors and a.said)
    assert "insufficient_quota" in a.errors[0] and "rate limit" not in a.errors[0].lower()
    assert "out of credit" in a.said[-1]
