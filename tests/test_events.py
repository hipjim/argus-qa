from argus_qa.events import EventLog, capture, describe_tool, emit


def test_emit_without_sink_is_a_no_op():
    emit("say", "nobody is listening")


def test_capture_and_read(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    with capture(log):
        emit("phase", "Starting")
        emit("action", "Click: Sign in", agent="Rita", tool="click")
    emit("say", "outside the capture")
    events, cursor = EventLog.read(log.path)
    assert [e["text"] for e in events] == ["Starting", "Click: Sign in"]
    assert events[1]["agent"] == "Rita" and events[1]["tool"] == "click"
    assert cursor == 2
    assert EventLog.read(log.path, after=1)[0][0]["text"] == "Click: Sign in"
    assert EventLog.read(tmp_path / "missing.jsonl") == ([], 0)


def test_read_stops_at_partial_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"kind": "phase"}\n{"kind": "sa')
    events, cursor = EventLog.read(path)
    assert len(events) == 1 and cursor == 1


def test_describe_tool():
    def text(tool, tool_input):
        return describe_tool(f"mcp__playwright__browser_{tool}", tool_input)[1]

    assert text("navigate", {"url": "https://a.test"}) == "Open: https://a.test"
    assert text("click", {"element": "Sign in button", "ref": "e3"}) == "Click: Sign in button"
    assert text("type", {"element": "Email", "text": "a@b.c"}) == 'Type: Email ← "a@b.c"'
    assert text("fill_form", {"fields": [{"name": "Email", "value": "x"}]}) == 'Fill form: Email ← "x"'
    assert text("snapshot", {}) == "Read page"


def test_describe_screenshot_carries_file():
    tool, text, extra = describe_tool("mcp__playwright__browser_take_screenshot", {"filename": "shots/a.png"})
    assert (tool, text, extra) == ("take_screenshot", "Screenshot: shots/a.png", {"file": "a.png"})


def test_describe_unknown_tool():
    assert describe_tool("mcp__other__sync_data", {}) == ("sync_data", "Sync data", {})


def test_prose_strips_json_report():
    from argus_qa.agents.orchestrator import _prose

    assert _prose('The test passed.\n\n```json\n{"a": 1}\n```') == "The test passed."
    assert _prose('```json\n{"summary": "x"}\n```') == ""
    assert _prose('{"summary": "x"}') == ""
    assert _prose("Now logging in as member.") == "Now logging in as member."


def test_shorten_titles():
    from argus_qa.server import _shorten

    assert _shorten("Explore: short", 90) == "Explore: short"
    long = "Explore: Try to break the login form with unusual input: empty fields, very long values, special characters"
    assert _shorten(long, 60) == "Explore: Try to break the login form with unusual input…"
