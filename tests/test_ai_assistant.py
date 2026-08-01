from unittest.mock import MagicMock

import google.genai
import pytest

import ai_assistant
from pawpal_system import Owner, Pet, Task
from ai_assistant import answer_question, answer_question_rule_based, retrieve_relevant_chunks


@pytest.fixture(autouse=True)
def isolate_gemini_key(monkeypatch, request):
    """Make tests deterministic regardless of any real GEMINI_API_KEY configured
    on the machine running them (env var or .streamlit/secrets.toml) — every test
    gets the rule-based path by default; tests that need Gemini active override
    `_get_gemini_api_key` explicitly in their own body.
    """
    if request.node.name == "test_get_gemini_api_key_reads_environment_variable":
        # This test exercises the real key-lookup function itself, so it clears
        # both real sources instead of replacing the function.
        monkeypatch.setattr(ai_assistant.st, "secrets", {}, raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: None)


def make_owner_with_pet(pet_name="Mochi", task=True):
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    pet = Pet(name=pet_name, species="Dog", breed="Shiba Inu", age=2, weight=15.0)
    if task:
        pet.add_task(Task(description="Morning walk", time="8:00 AM", frequency="daily"))
    owner.add_pet(pet)
    return owner, pet


class FakeSessionState(dict):
    """Minimal stand-in for st.session_state: supports both dict-style
    (`.get("owner")`) and attribute-style (`.owner = ...`) access, since the
    action handlers use both, matching how real Streamlit session_state works.
    """

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


def make_fake_function_call(name, args):
    call = MagicMock()
    call.name = name
    call.args = args
    return call


def make_fake_gemini_response(function_call=None, text=None):
    part = MagicMock()
    part.function_call = function_call
    part.text = text
    response = MagicMock()
    response.candidates = [MagicMock(content=MagicMock(parts=[part]))]
    return response


# --- No owner / no pets edge cases ---

def test_answers_gracefully_when_no_owner_exists():
    assert "no owner" in answer_question("what tasks does mochi have", None).lower()


def test_answers_gracefully_when_owner_has_no_pets():
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    result = answer_question("what tasks does mochi have", owner)
    assert "no pets" in result.lower()


# --- Greeting / help ---

def test_greeting_returns_example_questions():
    assert "PawPal+ assistant" in answer_question("hi", None)


def test_help_describes_capabilities():
    assert "scheduling conflicts" in answer_question("help", None).lower()


# --- Owner info ---

def test_owner_intent_returns_contact_info():
    owner, _ = make_owner_with_pet()
    result = answer_question("who is the owner", owner)
    assert "Jordan" in result and "jordan@email.com" in result


# --- Pet list ---

def test_pet_list_intent_lists_all_pets():
    owner, _ = make_owner_with_pet()
    result = answer_question("how many pets do I have", owner)
    assert "1 pet" in result and "Mochi" in result


# --- Conflicts ---

def test_conflict_intent_reports_no_conflicts():
    owner, _ = make_owner_with_pet()
    assert answer_question("are there any conflicts", owner) == "No scheduling conflicts detected."


def test_conflict_intent_reports_actual_conflict():
    owner, pet = make_owner_with_pet(task=False)
    pet.add_task(Task(description="Feed breakfast", time="8:00 AM", frequency="daily", date="2026-01-01"))
    pet.add_task(Task(description="Give medication", time="8:00 AM", frequency="daily", date="2026-01-01"))
    result = answer_question("any conflicts?", owner)
    assert "Mochi" in result and "8:00 AM" in result


# --- Schedule ---

def test_schedule_intent_returns_daily_schedule():
    owner, _ = make_owner_with_pet()
    assert "Daily Schedule" in answer_question("what's today's schedule", owner)


# --- Pet-specific: tasks, pending, completed ---

def test_pet_tasks_intent_lists_tasks_for_named_pet():
    owner, _ = make_owner_with_pet()
    result = answer_question("what tasks does mochi have", owner)
    assert "Mochi" in result and "Morning walk" in result


def test_pet_name_recognized_in_possessive_form():
    # Regression: the tokenizer used to glue "'s" onto the pet name (producing
    # "mochi's"), which never matched the bare pet name "mochi" and silently
    # broke natural phrasing like "What are Mochi's tasks?". ("schedule" is
    # avoided here since it triggers a separate, higher-priority intent.)
    owner, _ = make_owner_with_pet()
    result = answer_question("what are mochi's tasks", owner)
    assert "Mochi" in result and "Morning walk" in result


def test_pet_pending_intent_when_task_incomplete():
    owner, _ = make_owner_with_pet()
    result = answer_question("does mochi have pending tasks", owner)
    assert "Mochi" in result and "Morning walk" in result


def test_pet_pending_intent_when_all_caught_up():
    owner, pet = make_owner_with_pet()
    pet.tasks[0].mark_complete()
    assert "no pending tasks" in answer_question("does mochi have pending tasks", owner).lower()


def test_pet_completed_intent_lists_completed_tasks():
    owner, pet = make_owner_with_pet()
    pet.tasks[0].mark_complete()
    result = answer_question("is mochi done with anything", owner)
    assert "Mochi" in result and "Morning walk" in result


def test_pet_completed_intent_when_none_completed():
    owner, _ = make_owner_with_pet()
    assert "no completed tasks" in answer_question("what has mochi finished", owner).lower()


def test_pet_with_no_tasks_reports_that_directly():
    owner, _ = make_owner_with_pet(task=False)
    assert answer_question("what tasks does mochi have", owner) == "Mochi doesn't have any tasks scheduled yet."


# --- All-pets aggregate intents ---

def test_pending_intent_without_pet_name_totals_across_all_pets():
    owner, _ = make_owner_with_pet()
    assert "1 pending task" in answer_question("how many pending tasks are there", owner)


def test_task_word_without_pet_name_lists_every_task():
    owner, _ = make_owner_with_pet()
    result = answer_question("show me all tasks", owner)
    assert "Mochi" in result and "Morning walk" in result


# --- Fallback (retrieval) ---

def test_fallback_surfaces_relevant_data_directly_when_no_intent_matches():
    owner, _ = make_owner_with_pet()
    result = answer_question("tell me about jordan", owner)
    assert result == "The owner is Jordan, reachable at jordan@email.com or 555-0100."


def test_fallback_gives_guidance_when_nothing_is_relevant():
    owner, _ = make_owner_with_pet()
    result = answer_question("asdkjaskdj random gibberish", owner)
    assert "couldn't find anything relevant" in result.lower()


# --- Retrieval scoring ---

def test_retrieve_relevant_chunks_ranks_by_word_overlap():
    chunks = ["Mochi's tasks: Morning walk", "Buddy's tasks: Evening walk", "Owner info: Jordan"]
    ranked = retrieve_relevant_chunks("what does mochi have", chunks, top_k=1)
    assert ranked[0][0] == "Mochi's tasks: Morning walk"
    assert ranked[0][1] > 0


def test_retrieve_relevant_chunks_returns_zero_score_when_nothing_matches():
    ranked = retrieve_relevant_chunks("zzz qqq", ["Mochi's tasks: Morning walk"], top_k=1)
    assert ranked[0][1] == 0


# --- Guardrail: never raises or returns empty, even on degenerate input ---

def test_answer_question_never_raises_on_odd_input():
    owner, _ = make_owner_with_pet()
    for weird in ["", "   ", "???", "🐾🐾🐾", "a" * 500]:
        result = answer_question(weird, owner)
        assert isinstance(result, str)
        assert result != ""


# --- Hybrid dispatch: Gemini when a key is configured, rule-based otherwise ---

def test_get_gemini_api_key_reads_environment_variable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ai_assistant._get_gemini_api_key() is None

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-123")
    assert ai_assistant._get_gemini_api_key() == "fake-key-123"


def test_answer_question_uses_rule_based_path_when_no_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    owner, _ = make_owner_with_pet()
    query = "What tasks does Mochi have?"
    assert answer_question(query, owner) == answer_question_rule_based(query, owner)


def test_answer_question_routes_to_gemini_when_key_is_present(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner, _ = make_owner_with_pet()

    fake_response = MagicMock(text="Mochi has one task: a morning walk at 8 AM.")
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = answer_question("What tasks does Mochi have?", owner)

    assert result == "Mochi has one task: a morning walk at 8 AM."
    fake_client.models.generate_content.assert_called_once()
    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == ai_assistant.GEMINI_MODEL
    assert "Mochi" in call_kwargs["contents"]  # retrieved context was actually included


def test_gemini_failure_falls_back_to_rule_based_answer(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner, _ = make_owner_with_pet()

    broken_client = MagicMock()
    broken_client.models.generate_content.side_effect = RuntimeError("network down")
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=broken_client))

    query = "What tasks does Mochi have?"
    result = answer_question(query, owner)

    assert result == answer_question_rule_based(query, owner)


def test_gemini_empty_response_falls_back_to_a_safe_message(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner, _ = make_owner_with_pet()

    fake_response = MagicMock(text="")
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = answer_question("What tasks does Mochi have?", owner)

    assert isinstance(result, str) and result != ""


# --- Agentic actions: rule-based structured commands (offline mode) ---

def test_rule_based_action_creates_owner():
    session_state = FakeSessionState(owner=None)
    result = ai_assistant.handle_chat_message(
        'create owner name=Sarah email=sarah@email.com phone=555-1234', session_state
    )
    assert "Sarah" in result
    assert session_state.owner is not None
    assert session_state.owner.name == "Sarah"
    assert session_state.owner.email == "sarah@email.com"


def test_rule_based_action_create_owner_when_one_already_exists():
    owner, _ = make_owner_with_pet()
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message(
        'create owner name=Sarah email=sarah@email.com phone=555-1234', session_state
    )
    assert "Jordan" in result
    assert session_state.owner.name == "Jordan"  # unchanged


def test_rule_based_action_adds_pet():
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message(
        "add pet name=Rex species=Dog breed=Labrador age=3 weight=60", session_state
    )
    assert "Rex" in result
    assert len(owner.pets) == 1
    assert owner.pets[0].name == "Rex"
    assert owner.pets[0].age == 3
    assert owner.pets[0].weight == 60.0


def test_rule_based_action_add_pet_asks_for_missing_fields():
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message("add pet name=Rex", session_state)
    assert "species" in result.lower()
    assert len(owner.pets) == 0  # nothing was added


def test_rule_based_action_add_pet_without_owner():
    session_state = FakeSessionState(owner=None)
    result = ai_assistant.handle_chat_message(
        "add pet name=Rex species=Dog breed=Labrador age=3 weight=60", session_state
    )
    assert "owner" in result.lower()


def test_rule_based_action_adds_task():
    owner, pet = make_owner_with_pet(task=False)
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message(
        'add task pet=Mochi description="Feed dinner" time="6:00 PM" frequency=daily', session_state
    )
    assert "Mochi" in result and "Feed dinner" in result
    assert len(pet.tasks) == 1
    assert pet.tasks[0].description == "Feed dinner"
    assert pet.tasks[0].time == "6:00 PM"


def test_rule_based_action_add_task_rejects_invalid_time():
    owner, pet = make_owner_with_pet(task=False)
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message(
        'add task pet=Mochi description="Feed dinner" time="6pm" frequency=daily', session_state
    )
    assert "valid time" in result.lower()
    assert len(pet.tasks) == 0  # rejected, not added


def test_rule_based_action_add_task_unknown_pet():
    owner, _ = make_owner_with_pet()
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message(
        'add task pet=Buddy description="Walk" time="6:00 PM" frequency=daily', session_state
    )
    assert "Buddy" in result and "Mochi" in result  # tells the user what pets actually exist


def test_handle_chat_message_falls_through_to_normal_qa_when_not_an_action():
    owner, _ = make_owner_with_pet()
    session_state = FakeSessionState(owner=owner)
    result = ai_assistant.handle_chat_message("what tasks does mochi have", session_state)
    assert "Mochi" in result and "Morning walk" in result


# --- Agentic actions: Gemini function calling ---

def test_gemini_action_add_pet_executes_and_mutates_state(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    session_state = FakeSessionState(owner=owner)

    fn_call = make_fake_function_call(
        "add_pet", {"name": "Rex", "species": "dog", "breed": "Labrador", "age": 3, "weight": 60}
    )
    fake_response = make_fake_gemini_response(function_call=fn_call)
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = ai_assistant.handle_chat_message(
        "Please add a pet named Rex, a 3 year old Labrador that weighs 60 pounds.", session_state
    )

    assert "Rex" in result
    assert len(owner.pets) == 1
    assert owner.pets[0].name == "Rex"
    # Tools offered should match current state: no owner -> create_owner only;
    # owner exists, no pets -> add_pet only (not add_task yet).
    call_kwargs = fake_client.models.generate_content.call_args.kwargs
    tool_names = [fn.name for fn in call_kwargs["config"].tools[0].function_declarations]
    assert tool_names == ["add_pet"]


def test_gemini_action_add_task_executes_and_mutates_state(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner, pet = make_owner_with_pet(task=False)
    session_state = FakeSessionState(owner=owner)

    fn_call = make_fake_function_call(
        "add_task", {"pet_name": "Mochi", "description": "Feed dinner", "time": "6:00 PM", "frequency": "daily"}
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_gemini_response(function_call=fn_call)
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = ai_assistant.handle_chat_message("Schedule dinner for Mochi at 6pm daily.", session_state)

    assert "Mochi" in result and "Feed dinner" in result
    assert len(pet.tasks) == 1


def test_gemini_action_create_owner_executes_and_mutates_state(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    session_state = FakeSessionState(owner=None)

    fn_call = make_fake_function_call(
        "create_owner", {"name": "Sarah", "email": "sarah@email.com", "phone": "555-1234"}
    )
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_gemini_response(function_call=fn_call)
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = ai_assistant.handle_chat_message("Set me up as the owner, I'm Sarah, sarah@email.com, 555-1234.", session_state)

    assert "Sarah" in result
    assert session_state.owner is not None
    assert session_state.owner.name == "Sarah"


def test_gemini_plain_question_still_returns_text_not_an_action(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner, _ = make_owner_with_pet()
    session_state = FakeSessionState(owner=owner)

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_gemini_response(
        text="Mochi has one pending task: a daily morning walk at 8 AM."
    )
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = ai_assistant.handle_chat_message("What tasks does Mochi have?", session_state)

    assert result == "Mochi has one pending task: a daily morning walk at 8 AM."
    assert len(owner.pets) == 1  # nothing was mutated


def test_gemini_unrecognized_function_falls_back_to_rule_based(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner, _ = make_owner_with_pet()
    session_state = FakeSessionState(owner=owner)

    fn_call = make_fake_function_call("delete_everything", {})
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_gemini_response(function_call=fn_call)
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=fake_client))

    result = ai_assistant.handle_chat_message("What tasks does Mochi have?", session_state)

    # Falls back to the rule-based Q&A answer rather than crashing or acting
    assert "Mochi" in result and "Morning walk" in result


def test_gemini_failure_during_action_turn_falls_back_to_rule_based_action(monkeypatch):
    monkeypatch.setattr(ai_assistant, "_get_gemini_api_key", lambda: "fake-key-123")
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    session_state = FakeSessionState(owner=owner)

    broken_client = MagicMock()
    broken_client.models.generate_content.side_effect = RuntimeError("network down")
    monkeypatch.setattr(google.genai, "Client", MagicMock(return_value=broken_client))

    # Gemini is unreachable, but the rule-based structured command still works
    result = ai_assistant.handle_chat_message(
        "add pet name=Rex species=Dog breed=Labrador age=3 weight=60", session_state
    )

    assert "Rex" in result
    assert len(owner.pets) == 1
