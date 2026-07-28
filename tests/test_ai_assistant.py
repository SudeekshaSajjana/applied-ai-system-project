from pawpal_system import Owner, Pet, Task
from ai_assistant import answer_question, retrieve_relevant_chunks


def make_owner_with_pet(pet_name="Mochi", task=True):
    owner = Owner(name="Jordan", email="jordan@email.com", phone="555-0100")
    pet = Pet(name=pet_name, species="Dog", breed="Shiba Inu", age=2, weight=15.0)
    if task:
        pet.add_task(Task(description="Morning walk", time="8:00 AM", frequency="daily"))
    owner.add_pet(pet)
    return owner, pet


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
    assert "Mochi's tasks" in result and "Morning walk" in result


def test_pet_name_recognized_in_possessive_form():
    # Regression: the tokenizer used to glue "'s" onto the pet name (producing
    # "mochi's"), which never matched the bare pet name "mochi" and silently
    # broke natural phrasing like "What are Mochi's tasks?". ("schedule" is
    # avoided here since it triggers a separate, higher-priority intent.)
    owner, _ = make_owner_with_pet()
    result = answer_question("what are mochi's tasks", owner)
    assert "Mochi's tasks" in result and "Morning walk" in result


def test_pet_pending_intent_when_task_incomplete():
    owner, _ = make_owner_with_pet()
    result = answer_question("does mochi have pending tasks", owner)
    assert "pending tasks" in result.lower() and "Morning walk" in result


def test_pet_pending_intent_when_all_caught_up():
    owner, pet = make_owner_with_pet()
    pet.tasks[0].mark_complete()
    assert "no pending tasks" in answer_question("does mochi have pending tasks", owner).lower()


def test_pet_completed_intent_lists_completed_tasks():
    owner, pet = make_owner_with_pet()
    pet.tasks[0].mark_complete()
    result = answer_question("is mochi done with anything", owner)
    assert "completed tasks" in result.lower() and "Morning walk" in result


def test_pet_completed_intent_when_none_completed():
    owner, _ = make_owner_with_pet()
    assert "no completed tasks" in answer_question("what has mochi finished", owner).lower()


def test_pet_with_no_tasks_reports_that_directly():
    owner, _ = make_owner_with_pet(task=False)
    assert answer_question("what tasks does mochi have", owner) == "Mochi has no tasks scheduled."


# --- All-pets aggregate intents ---

def test_pending_intent_without_pet_name_totals_across_all_pets():
    owner, _ = make_owner_with_pet()
    assert "1 pending task" in answer_question("how many pending tasks are there", owner)


def test_task_word_without_pet_name_lists_every_task():
    owner, _ = make_owner_with_pet()
    result = answer_question("show me all tasks", owner)
    assert "All tasks" in result and "Mochi: " in result


# --- Fallback (retrieval) ---

def test_fallback_surfaces_relevant_data_directly_when_no_intent_matches():
    owner, _ = make_owner_with_pet()
    result = answer_question("tell me about jordan", owner)
    assert result == "Owner contact info: Jordan | Email: jordan@email.com | Phone: 555-0100."


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
