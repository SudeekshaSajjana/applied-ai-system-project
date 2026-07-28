from streamlit.testing.v1 import AppTest


def _launch():
    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    return at


def _create_owner(at):
    next(b for b in at.button if b.label == "Create Owner").click().run(timeout=30)
    return at


def _add_pet(at):
    next(b for b in at.button if b.label == "Add Pet").click().run(timeout=30)
    return at


def _add_task(at):
    next(b for b in at.button if b.label == "Add Task").click().run(timeout=30)
    return at


def _time_input(at):
    return next(t for t in at.text_input if t.label == "Time")


def test_app_loads_without_exceptions():
    at = _launch()
    assert not at.exception


# --- End-to-end workflow: Create Owner -> Add Pet -> Schedule a Task ---

def test_end_to_end_owner_pet_task_workflow():
    at = _launch()

    _create_owner(at)
    assert "Owner 'Jordan' created!" in [s.value for s in at.success]

    _add_pet(at)
    assert "Mochi added!" in [s.value for s in at.success]

    _add_task(at)
    assert not at.exception
    assert "Task 'Morning walk' added to Mochi!" in [s.value for s in at.success]
    assert at.metric[0].value == "1"


# --- Guardrail: invalid task time is rejected, not a crash ---

def test_invalid_task_time_is_rejected_without_crashing():
    at = _launch()
    _create_owner(at)
    _add_pet(at)

    _time_input(at).set_value("8am").run(timeout=30)
    _add_task(at)

    assert not at.exception
    assert any("isn't a valid time" in e.value for e in at.error)


def test_valid_task_time_is_accepted_after_a_rejection():
    at = _launch()
    _create_owner(at)
    _add_pet(at)

    _time_input(at).set_value("8am").run(timeout=30)
    _add_task(at)

    _time_input(at).set_value("8:00 AM").run(timeout=30)
    _add_task(at)

    assert not at.exception
    assert "Task 'Morning walk' added to Mochi!" in [s.value for s in at.success]


# --- AI Assistant: launcher, chat survives submission, closes only via "X" ---

def test_ai_assistant_opens_and_stays_open_across_chat_submission():
    at = _launch()
    _create_owner(at)
    _add_pet(at)
    _add_task(at)

    next(b for b in at.button if b.key == "ai_launcher_btn").click().run(timeout=30)
    assert at.session_state["ai_chat_open"] is True

    at.chat_input[0].set_value("What tasks does Mochi have?").run(timeout=30)
    assert not at.exception
    assert at.session_state["ai_chat_open"] is True

    messages = [m.markdown[0].value for m in at.chat_message if m.markdown]
    assert "Mochi's tasks" in messages[-1]


def test_ai_assistant_closes_only_via_close_button():
    at = _launch()
    _create_owner(at)
    _add_pet(at)

    next(b for b in at.button if b.key == "ai_launcher_btn").click().run(timeout=30)
    at.chat_input[0].set_value("hi").run(timeout=30)
    assert at.session_state["ai_chat_open"] is True

    next(b for b in at.button if b.key == "ai_close_btn").click().run(timeout=30)
    assert at.session_state["ai_chat_open"] is False
