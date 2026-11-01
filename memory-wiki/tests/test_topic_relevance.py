"""User evidence controls topics across extraction and the read-only API."""

import pytest


@pytest.mark.parametrize("user_text", [
    'Please compare "vegetable and flower gardens" for spring planting.',
    'Please compare Vegetable And Flower Gardens for spring planting.',
])
def test_normalized_phrase_api_prefers_original_user(plugin_api, hermes_home, user_text):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_state import SessionDB

    path = hermes_home / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session("phrase", "cli")
        db.append_message("phrase", "user", user_text)
        db.append_message("phrase", "assistant", "Implemented vegetable flower gardens.")
    finally:
        db.close()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/wiki")
    with TestClient(app) as client:
        response = client.get("/wiki/subjects/vegetable-flower-gardens")
        assert response.status_code == 200
        subject = response.json()["subject"]
        assert [(s["role"], s["text"]) for s in subject["snippets"]] == [
            ("user", user_text),
            ("assistant", "Implemented vegetable flower gardens."),
        ]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


NOISE = [
    {"role": role, "content": "Implemented PhantomWidget in secret/noise.py with flask",
     "tool_name": "hidden_tool", "tool_calls": [{"function": {"name": "hidden_call"}}]}
    for role in ("assistant", "tool", "system", "developer")
]


def test_machine_repetition_cannot_create_or_score_topics(memory_wiki_core):
    extract = memory_wiki_core.extract_subject_candidates
    session = {"title": "Garden planning", "preview": "PreviewGhost package/noise.py"}
    users = [{"role": "user", "content": "discuss vegetable gardens"}]
    assert extract(session, users + NOISE * 20) == extract(session, users)
    assert extract({}, NOISE) == []
    assert extract({"preview": "PreviewGhost package/noise.py"}, []) == []


def test_all_user_turns_and_explicit_identifiers(memory_wiki_core):
    candidates = memory_wiki_core.extract_subject_candidates({}, [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "discuss vegetable gardens"},
        {"role": "user", "content": "search_files tools.lookup web/src/App.tsx pytest"},
    ])
    assert {c.name for c in candidates} >= {
        "vegetable gardens", "search_files", "tools.lookup", "web/src/App.tsx", "pytest",
    }


@pytest.mark.parametrize("messages", [[], [{"role": "user", "content": ""}], NOISE])
def test_title_fallback(memory_wiki_core, messages):
    candidates = memory_wiki_core.extract_subject_candidates({"title": "Garden planning"}, messages)
    assert [c.slug for c in candidates] == ["garden-planning"]


def test_api_relevance_and_read_only(plugin_api, hermes_home):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_state import SessionDB

    path = hermes_home / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session("garden", "cli")
        db.set_session_title("garden", "Aardvark planning")
        for message in NOISE * 3:
            db.append_message("garden", message["role"], message["content"],
                              tool_name=message["tool_name"], tool_calls=message["tool_calls"])
        db.append_message("garden", "assistant", "Implemented vegetable gardens")
        db.append_message("garden", "user", "discuss vegetable gardens")
        db.append_message("garden", "assistant", "Implemented unrelated changes")
    finally:
        db.close()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/wiki")
    with TestClient(app) as client:
        overview = client.get("/wiki/overview?subject_limit=1").json()
        assert [s["slug"] for s in overview["subjects"]] == ["vegetable-gardens"]
        subjects = client.get("/wiki/subjects").json()["subjects"]
        assert {s["slug"] for s in subjects} == {"vegetable-gardens", "aardvark-planning"}
        assert client.get("/wiki/subjects?q=phantom").json()["subjects"] == []
        detail = client.get("/wiki/subjects/vegetable-gardens").json()["subject"]
        assert detail["snippets"][0]["role"] == "user"
        assert all(s["role"] in ("user", "assistant") for s in detail["snippets"])
        assert detail["message_count"] == 15
        assert client.get("/wiki/subjects/aardvark-planning").json()["subject"]["snippets"] == []
        days = client.get("/wiki/days").json()["daily_logs"]
        day = client.get(f'/wiki/days/{days[0]["date"]}').json()["daily_log"]
        assert day == days[0]
        assert {s["slug"] for s in day["subjects"]} == {s["slug"] for s in subjects}
        assert [w["text"] for w in day["work_items"]] == [
            "Implemented vegetable gardens", "discuss vegetable gardens",
        ]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_aggregate_ranking_and_daily_limit_ignore_machine_repetition(memory_wiki_core, hermes_home):
    from hermes_state import SessionDB

    db = SessionDB(db_path=hermes_home / "state.db")
    try:
        for index in range(22):
            session_id = f"title-{index}"
            db.create_session(session_id, "cli")
            db.set_session_title(session_id, f"Aardvark planning topic{index}")
        db.create_session("user", "cli")
        db.append_message("user", "user", "discuss vegetable gardens")
        db.append_message("user", "user", "discuss winter planting")
        before = memory_wiki_core.build_memory_subjects(db)
        day_before = memory_wiki_core.build_daily_logs(db)[0]
        assert day_before["subjects"][0]["slug"] == "vegetable-gardens"
        assert day_before["subjects"][1]["slug"] == "winter-planting"
        assert len(day_before["subjects"]) == 20
        for _ in range(12):
            db.append_message("user", "assistant", "Implemented vegetable gardens")
            db.append_message("title-0", "assistant", "Implemented unrelated changes")
        after = memory_wiki_core.build_memory_subjects(db)
        assert [s["slug"] for s in after] == [s["slug"] for s in before]
        assert memory_wiki_core.build_memory_subjects(db, limit=1)[0]["slug"] == "vegetable-gardens"
        assert after[0]["snippets"][0]["role"] == "user"
        day_after = memory_wiki_core.build_daily_logs(db)[0]
        assert day_after["subjects"] == day_before["subjects"]
        assert len(day_after["work_items"]) == 3
    finally:
        db.close()


def test_user_metadata_is_not_evidence(memory_wiki_core):
    extract = memory_wiki_core.extract_subject_candidates
    user = {"role": "user", "content": "discuss vegetable gardens"}
    assert extract({}, [user]) == extract({}, [dict(
        user, tool_name="hidden_tool", tool_calls=[{"name": "hidden_call"}],
    )])


def test_structured_content_api_round_trip(plugin_api, hermes_home):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_state import SessionDB

    excluded = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,PhantomWidget"},
         "text": "Implemented PhantomWidget"},
        {"type": "metadata", "text": "Implemented PhantomWidget"},
        {"text": "Implemented PhantomWidget"},
        {"type": "text", "text": {"value": "PhantomWidget"}},
        "Implemented PhantomWidget",
    ]
    user_content = [{"type": "text", "text": "discuss vegetable"},
                    {"type": "text", "text": "gardens", "metadata": "PhantomWidget"}] + excluded
    path = hermes_home / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session("structured", "cli")
        db.append_message("structured", "user", user_content)
        db.append_message("structured", "assistant", [
            {"type": "text", "text": "Implemented vegetable gardens"},
        ] + excluded)
        db.append_message("structured", "assistant", [
            {"type": "text", "text": "Implemented PhantomWidget"},
        ])
        for role in ("user", "assistant"):
            db.append_message("structured", role, excluded)
        for role in ("tool", "system", "developer"):
            db.append_message("structured", role, [
                {"type": "text", "text": "Implemented vegetable gardens PhantomWidget"},
            ])
        assert db.get_messages("structured")[0]["content"] == user_content
    finally:
        db.close()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/wiki")
    with TestClient(app) as client:
        overview = client.get("/wiki/overview").json()
        subjects = client.get("/wiki/subjects").json()["subjects"]
        assert [s["slug"] for s in subjects] == ["vegetable-gardens"]
        assert overview["subjects"] == subjects
        assert client.get("/wiki/subjects?q=phantom").json()["subjects"] == []
        detail = client.get("/wiki/subjects/vegetable-gardens").json()["subject"]
        assert [(s["role"], s["text"]) for s in detail["snippets"]] == [
            ("user", "discuss vegetable gardens"),
            ("assistant", "Implemented vegetable gardens"),
        ]
        days = client.get("/wiki/days").json()["daily_logs"]
        day = client.get(f'/wiki/days/{days[0]["date"]}').json()["daily_log"]
        assert day == days[0] == overview["daily_logs"][0]
        assert [s["slug"] for s in day["subjects"]] == ["vegetable-gardens"]
        assert {w["text"] for w in day["work_items"]} == {
            "discuss vegetable gardens", "Implemented vegetable gardens",
        }
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("assistant_text", ["", "Implemented", "Implemented unrelated changes", "...", "!!!", "✅"])
def test_subject_topic_rejects_unrelated_assistant(memory_wiki_core, hermes_home, assistant_text):
    from hermes_state import SessionDB

    db = SessionDB(db_path=hermes_home / "state.db")
    try:
        db.create_session("subject", "cli")
        db.append_message("subject", "user", "/subject")
        db.append_message("subject", "assistant", assistant_text)
        subject = memory_wiki_core.get_memory_subject(db, "subject")
        assert [(s["role"], s["text"]) for s in subject["snippets"]] == [("user", "/subject")]
        assert [w["text"] for w in memory_wiki_core.build_daily_logs(db)[0]["work_items"]] == ["/subject"]
        db.append_message("subject", "assistant", "Implemented /subject")
        subject = memory_wiki_core.get_memory_subject(db, "subject")
        assert [s["text"] for s in subject["snippets"]] == ["/subject", "Implemented /subject"]
        assert {w["text"] for w in memory_wiki_core.build_daily_logs(db)[0]["work_items"]} == {
            "/subject", "Implemented /subject",
        }
    finally:
        db.close()
