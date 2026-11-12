import pytest
from pathlib import Path
from core.memory import Memory, looks_sensitive
from core.command_router import CommandRouter
from database.database import Database
from automation import file_manager
from speech.wake_word import detect


@pytest.fixture
def mem(tmp_path):
    return Memory(Database(tmp_path / "t.db"))


@pytest.fixture
def router(mem):
    return CommandRouter(mem)


def test_memory_crud_and_settings(mem):
    i = mem.add_memory("I prefer dark mode")
    mem.update_memory(i, "I prefer light mode")
    assert mem.list_memories()[0]["content"] == "I prefer light mode"
    mem.delete_memory(i)
    assert mem.list_memories() == []
    assert mem.get("tts_speed") == "1.0"
    mem.set("tts_speed", 1.1)
    assert mem.get_float("tts_speed") == 1.1
    mem.record_command("time"); mem.record_command("time")
    assert mem.frequent_commands()[0] == {"command": "time", "count": 2}


def test_sensitive_detection():
    assert looks_sensitive("my bank account number is 1234")
    assert looks_sensitive("my card 4111111111111111")
    assert not looks_sensitive("I like green tea")


@pytest.mark.parametrize("text,expect", [
    ("what's the time", "It's"), ("what is the date today", "Today is"),
    ("tell me the battery status", "attery"), ("cpu usage", "CPU"),
    ("samay batao", "It's"),
])
def test_info_commands(router, text, expect):
    r = router.route(text)
    assert r.handled and expect in r.reply


def test_unrecognised_goes_to_llm(router):
    assert not router.route("why is the sky blue").handled
    assert not router.route("explain recursion in python").handled


def test_remember_and_sensitive_confirmation(router, mem):
    assert router.route("Remember that I like jazz.").handled
    assert mem.list_memories()[0]["content"] == "I like jazz"
    r = router.route("remember that my password is hunter2")
    assert r.pending and len(mem.list_memories()) == 1          # NOT stored yet
    assert router.resolve_pending("no").reply.startswith("Okay, cancelled")
    assert len(mem.list_memories()) == 1
    router.route("remember that my pin is 4321")
    router.resolve_pending("yes")
    assert len(mem.list_memories()) == 2


def test_forget_requires_confirmation(router, mem):
    mem.add_memory("likes jazz")
    r = router.route("forget jazz")
    assert r.pending and len(mem.list_memories()) == 1
    router.resolve_pending("haan")
    assert mem.list_memories() == []


def test_confirmation_ignores_unrelated_speech(router, mem):
    router.route("forget nothing-here")           # no match -> no pending
    mem.add_memory("x1")
    router.route("forget x1")
    assert router.resolve_pending("what's the weather") is None and router.has_pending()


def test_close_app_needs_confirmation_and_allowlist(router):
    r = router.route("close chrome")
    assert r.pending and "Close chrome" in r.pending.prompt
    router.cancel_pending()
    r2 = router.route("close some random program")
    assert r2.handled and not r2.pending and "supported list" in r2.reply


def test_stop_command_interrupts(router):
    assert router.route("stop").interrupt
    assert router.route("ruko").interrupt


def test_web_commands(router, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u) or True)
    router.route("search google for python tutorials")
    router.route("search cats on youtube")
    router.route("youtube pe lofi music chalao")
    router.route("open github.com")
    router.route("open youtube")
    assert opened[0] == "https://www.google.com/search?q=python+tutorials"
    assert opened[1] == "https://www.youtube.com/results?search_query=cats"
    assert "lofi" in opened[2] and opened[3] == "https://github.com" and opened[4].startswith("https://www.youtube")


def test_open_app_routes(router, monkeypatch):
    called = []
    monkeypatch.setattr("automation.applications.open_app", lambda n: called.append(n) or f"Opening {n}.")
    for phrase in ("open notepad", "launch spotify", "calculator kholo", "please open vs code"):
        router.route(phrase)
    assert called == ["notepad", "spotify", "calculator", "vs code"]


def test_non_web_urls_blocked():
    from automation import browser
    with pytest.raises(ValueError):
        browser._open("file:///C:/Windows/System32/cmd.exe")
    assert browser.open_website("not a site at all") is None


def test_file_manager_confinement_and_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(file_manager, "HOME", tmp_path)
    monkeypatch.setattr(file_manager, "SEARCH_ROOTS", [tmp_path / "Documents"])
    (tmp_path / "Documents").mkdir()
    ws = tmp_path / "Documents" / "LUFY"
    assert "Created folder" in file_manager.create_folder("Projects", ws)
    assert "Created notes.txt" in file_manager.create_text_file("notes", "hi", ws)
    with pytest.raises(file_manager.FileError):
        file_manager.create_text_file("notes", "again", ws)
    with pytest.raises(file_manager.FileError):
        file_manager.create_folder("x", Path("/etc"))                 # outside home
    with pytest.raises(file_manager.FileError):
        file_manager.create_folder("..", ws)                          # traversal-ish name
    hits = file_manager.search_files("notes")
    assert hits and hits[0].name == "notes.txt"
    assert "Renamed" in file_manager.rename(hits[0], "todo")
    assert (ws / "todo.txt").exists()
    # organize plan is non-destructive until applied
    dl = tmp_path / "Downloads"; dl.mkdir()
    (dl / "a.png").write_bytes(b"1"); (dl / "b.pdf").write_bytes(b"1"); (dl / "c.xyz").write_bytes(b"1")
    plan = file_manager.plan_organize(dl)
    assert (dl / "a.png").exists()
    file_manager.apply_organize(plan)
    assert (dl / "Images" / "a.png").exists() and (dl / "Documents" / "b.pdf").exists() and (dl / "Other" / "c.xyz").exists()


def test_delete_goes_through_confirmation(router, tmp_path, monkeypatch):
    monkeypatch.setattr(file_manager, "HOME", tmp_path)
    monkeypatch.setattr(file_manager, "SEARCH_ROOTS", [tmp_path])
    f = tmp_path / "secret_plan.txt"; f.write_text("x")
    r = router.route("delete secret plan")
    assert r.pending and f.exists()


@pytest.mark.parametrize("text,matched,cmd", [
    ("Hey Lufy open chrome", True, "open chrome"), ("hey lucy what's the time", True, "what's the time"),
    ("Lufy", True, ""), ("ok loofy, battery", True, "battery"), ("हे लूफी समय बताओ", True, "समय बताओ"),
    ("I like lucy a lot", False, ""), ("open chrome", False, ""),
    ("Lufy open chrome", True, "open chrome"), ("Loofy what's the time", True, "what's the time"),
    ("loo fy open notepad", True, "open notepad"), ("lu fi, volume up", True, "volume up"),
    ("Luffy tell me a joke", True, "tell me a joke"), ("hey there loofy battery", True, "battery"),
    ("loofy.", True, ""), ("please open chrome", False, ""), ("look at this", False, ""),
])
def test_wake_word(text, matched, cmd):
    r = detect(text)
    assert r.matched == matched and r.command == cmd


@pytest.mark.parametrize("phrase", [
    "minimize Chrome", "minimise Chrome", "Chrome minimize", "Chrome ko minimize kar do",
    "minimize the current window", "Chrome chhota kar", "maximize Chrome", "restore Chrome",
    "focus Chrome", "bring Chrome to front", "Chrome ko saamne lao",
])
def test_window_commands_route_before_llm(router, monkeypatch, phrase):
    calls = []
    monkeypatch.setattr("automation.applications.minimize_window", lambda n: calls.append(("minimize", n)) or "Chrome minimized.")
    monkeypatch.setattr("automation.applications.maximize_window", lambda n: calls.append(("maximize", n)) or "Chrome maximized.")
    monkeypatch.setattr("automation.applications.restore_window", lambda n: calls.append(("restore", n)) or "Chrome restored.")
    monkeypatch.setattr("automation.applications.focus_window", lambda n: calls.append(("focus", n)) or "Brought Chrome to the front.")
    result = router.route(phrase)
    assert result.handled
    assert calls, f"No window function called for: {phrase}"


def test_minimize_chrome_routes_to_minimize_not_llm(router, monkeypatch):
    called = []
    monkeypatch.setattr("automation.applications.minimize_window", lambda name: called.append(name) or "Chrome minimized.")
    result = router.route("Hey Lufy, minimize Chrome")
    assert result.handled is True
    assert result.reply == "Chrome minimized."
    assert called == ["chrome"]


def test_spotify_search_routes_to_spotify(router, monkeypatch):
    called = []
    monkeypatch.setattr("automation.spotify.search_spotify", lambda query: called.append(query) or f"Opened Spotify search for {query}.")
    result = router.route("play Believer on Spotify")
    assert result.handled is True
    assert called == ["believer"]
    assert "Spotify search" in result.reply
