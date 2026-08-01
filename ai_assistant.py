"""Hybrid, agentic AI chatbot for PawPal+.

Answers are always grounded in the live owner/pet/task data already held in
Streamlit session state — never invented. Two answering modes:

- If a `GEMINI_API_KEY` is configured, questions are answered by Gemini using
  retrieval-augmented generation, and the assistant can also *act*: create an
  owner profile, add a pet, or schedule a task, via Gemini function calling —
  the model decides which function to call and with what arguments, and this
  module actually performs the mutation against the live app state.
- Otherwise (no key configured — the default), a rule-based intent matcher
  answers questions directly from the same data, and a small structured
  command syntax (e.g. "add pet name=Rex species=Dog breed=Labrador age=3
  weight=60") triggers the same underlying actions with no external API, no
  network call, and no cost. Gemini failures (missing package, bad key,
  network error) also fall back to this path automatically, so the assistant
  never goes silent.
"""

import logging
import os
import re
from datetime import datetime
from typing import List, Optional

import streamlit as st

from pawpal_system import Owner, Pet, Scheduler, Task

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-flash-lite-latest"

_GEMINI_BASE_INSTRUCTIONS = (
    "You are the PawPal+ assistant — a friendly, conversational helper embedded "
    "in a pet-care scheduling app. Talk like a helpful person, not a printout: "
    "rephrase and summarize the context in your own words rather than copying "
    "it verbatim, and vary your phrasing naturally. Base every answer ONLY on "
    "the facts in the context below — never invent pets, tasks, or times. If "
    "the answer isn't in the context, say so plainly instead of guessing."
)

GEMINI_SYSTEM_PROMPT = _GEMINI_BASE_INSTRUCTIONS + "\n\nContext:\n{context}"

GEMINI_AGENT_SYSTEM_PROMPT = (
    _GEMINI_BASE_INSTRUCTIONS + "\n\n"
    "If the user asks you to add a pet, schedule a task, or set up an owner "
    "profile, call the matching function to actually perform it instead of "
    "just describing what you would do.\n\n"
    "Context:\n{context}"
)


def _get_gemini_api_key() -> Optional[str]:
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY")

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


def _join_naturally(items: List[str]) -> str:
    items = list(items)
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _describe_task(task, include_status: bool = True) -> str:
    base = f"{task.description} at {task.time} ({task.frequency})"
    if include_status:
        return f"{base} — {'done' if task.completed else 'pending'}"
    return base


GREETING_WORDS = {"hi", "hello", "hey", "yo", "sup", "greetings"}
HELP_WORDS = {"help", "capabilities", "commands"}
CONFLICT_WORDS = {"conflict", "conflicts", "clash", "clashing", "overlap", "overlapping"}
SCHEDULE_WORDS = {"schedule", "today", "plan", "agenda"}
PENDING_WORDS = {"pending", "remaining", "left", "todo", "incomplete", "outstanding"}
COMPLETED_WORDS = {"completed", "done", "finished"}
OWNER_WORDS = {"owner", "contact", "email", "phone"}
PET_LIST_WORDS = {"pets", "animals"}
TASK_WORDS = {"task", "tasks", "chore", "chores", "walk", "walks", "feed", "feeding", "groom", "grooming"}


def _find_mentioned_pet(tokens: List[str], owner: Owner):
    for pet in owner.pets:
        if pet.name.lower() in tokens:
            return pet
    return None


def build_knowledge_base(owner: Optional[Owner]) -> List[str]:
    """Turn the current app state into short, retrievable text chunks (fallback use only)."""
    if owner is None:
        return ["No owner has been created yet, and no pets or tasks exist in the app yet."]

    chunks = [f"The owner is {owner.name}, reachable at {owner.email} or {owner.phone}."]

    if not owner.pets:
        chunks.append("The owner hasn't registered any pets yet.")
        return chunks

    scheduler = Scheduler(owner)

    for pet in owner.pets:
        chunks.append(
            f"{pet.name} is a {pet.age}-year-old {pet.species} ({pet.breed}), weighing {pet.weight} lbs."
        )
        if not pet.tasks:
            chunks.append(f"{pet.name} has no tasks scheduled.")
            continue
        task_descs = [_describe_task(t) for t in pet.tasks]
        chunks.append(f"{pet.name}'s tasks: " + _join_naturally(task_descs) + ".")
        pending_count = len(pet.get_pending_tasks())
        chunks.append(f"{pet.name} has {pending_count} pending task{'s' if pending_count != 1 else ''}.")

    conflicts = scheduler.detect_conflicts()
    chunks.append(
        "Scheduling conflicts: " + " ".join(conflicts) if conflicts
        else "There are no scheduling conflicts across any pet."
    )
    chunks.append(scheduler.get_daily_schedule())
    total_pending = len(scheduler.get_all_pending_tasks())
    chunks.append(
        f"There {'is' if total_pending == 1 else 'are'} {total_pending} "
        f"pending task{'s' if total_pending != 1 else ''} across all pets."
    )
    return chunks


def retrieve_relevant_chunks(query: str, chunks: List[str], top_k: int = 3) -> List[tuple]:
    """Lexical fallback retrieval: rank chunks by overlapping query words.

    Returns (chunk, score) pairs, highest score first. A score of 0 means no
    word in the chunk matched the query at all — callers use that to tell
    "found something relevant" apart from "found nothing."
    """
    query_words = set(_tokenize(query))
    if not query_words:
        return [(c, 0) for c in chunks[:top_k]]

    scored = [(c, sum(1 for w in _tokenize(c) if w in query_words)) for c in chunks]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]


def answer_question_rule_based(query: str, owner: Optional[Owner]) -> str:
    """Answer a question using rule-based intent matching over live app data."""
    tokens = set(_tokenize(query))

    if tokens & GREETING_WORDS:
        return (
            "Hi! I'm the PawPal+ assistant. I can tell you about your pets, tasks, "
            "and schedule, and I can take action too — add a pet, schedule a task, "
            "or set up an owner profile. Just tell me what you'd like to do."
        )

    if tokens & HELP_WORDS:
        return (
            "I can answer questions about your pets, tasks, pending/completed status, "
            "scheduling conflicts, and today's schedule — and I can act on your "
            "behalf too, adding a pet, scheduling a task, or setting up an owner "
            "profile, based on what's currently in the app."
        )

    if owner is None:
        return "No owner has been created yet — add one in the form above and I can help from there."

    scheduler = Scheduler(owner)

    if tokens & OWNER_WORDS:
        return f"You're {owner.name} — reachable at {owner.email} or {owner.phone}."

    if tokens & PET_LIST_WORDS:
        if not owner.pets:
            return "You haven't registered any pets yet."
        descs = [f"{p.name}, a {p.age}-year-old {p.species} ({p.breed})" for p in owner.pets]
        n = len(owner.pets)
        return f"You have {n} pet{'s' if n != 1 else ''}: " + _join_naturally(descs) + "."

    if tokens & CONFLICT_WORDS:
        conflicts = scheduler.detect_conflicts()
        return "\n".join(conflicts) if conflicts else "No scheduling conflicts detected."

    if tokens & SCHEDULE_WORDS and not (tokens & PENDING_WORDS or tokens & COMPLETED_WORDS):
        return scheduler.get_daily_schedule()

    if not owner.pets:
        return "No pets have been registered yet — add one above and ask me again."

    pet = _find_mentioned_pet(tokens, owner)

    if pet is not None:
        if tokens & PENDING_WORDS:
            pending = pet.get_pending_tasks()
            if not pending:
                return f"{pet.name} has no pending tasks — all caught up!"
            descs = [_describe_task(t, include_status=False) for t in pending]
            return f"{pet.name} still needs to: " + _join_naturally(descs) + "."

        if tokens & COMPLETED_WORDS:
            done = [t for t in pet.tasks if t.completed]
            if not done:
                return f"{pet.name} has no completed tasks yet."
            descs = [_describe_task(t, include_status=False) for t in done]
            return f"{pet.name} has finished: " + _join_naturally(descs) + "."

        if not pet.tasks:
            return f"{pet.name} doesn't have any tasks scheduled yet."
        n = len(pet.tasks)
        descs = [_describe_task(t) for t in pet.tasks]
        return f"{pet.name} has {n} task{'s' if n != 1 else ''}: " + _join_naturally(descs) + "."

    if tokens & PENDING_WORDS:
        total = len(scheduler.get_all_pending_tasks())
        return f"There {'is' if total == 1 else 'are'} {total} pending task{'s' if total != 1 else ''} across all your pets."

    if tokens & TASK_WORDS:
        all_tasks = owner.get_all_tasks()
        if not all_tasks:
            return "No tasks have been added for any pet yet."
        descs = [f"{p.name}'s {_describe_task(t)}" for p in owner.pets for t in p.tasks]
        return "Here's everything scheduled: " + _join_naturally(descs) + "."

    # Fallback: no recognized intent — actively use the single best-matching
    # retrieved chunk AS the answer (not dumped alongside a disclaimer). Only
    # fall back to guidance when nothing in the app's data is actually relevant.
    chunks = build_knowledge_base(owner)
    best_chunk, best_score = retrieve_relevant_chunks(query, chunks, top_k=1)[0]
    logger.info("Fallback retrieval: query=%r confidence_score=%d", query, best_score)

    if best_score > 0:
        return best_chunk

    return (
        "I couldn't find anything relevant to that in the app's current data. "
        "Try asking about a pet's tasks, pending/completed status, "
        "scheduling conflicts, or today's schedule."
    )


def generate_gemini_answer(query: str, owner: Optional[Owner]) -> str:
    """Answer using Gemini, grounded in retrieved app data (RAG). Question-answering only.

    Falls back to the rule-based answer on any failure (missing package,
    bad/missing key, network error) so the assistant never goes silent.
    """
    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai not installed; falling back to rule-based answer")
        return answer_question_rule_based(query, owner)

    chunks = build_knowledge_base(owner)
    relevant = retrieve_relevant_chunks(query, chunks, top_k=6)
    context = "\n\n".join(chunk for chunk, _score in relevant)

    try:
        client = genai.Client(api_key=_get_gemini_api_key())
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{GEMINI_SYSTEM_PROMPT.format(context=context)}\n\nQuestion: {query}",
        )
        text = (response.text or "").strip()
        return text or "I didn't get a usable response back from Gemini — please try again."
    except Exception:
        logger.exception("Gemini request failed for query=%r; falling back to rule-based answer", query)
        return answer_question_rule_based(query, owner)


def answer_question(query: str, owner: Optional[Owner]) -> str:
    """Answer a question (no actions) — via Gemini (RAG) if GEMINI_API_KEY is set, else rule-based."""
    if _get_gemini_api_key():
        return generate_gemini_answer(query, owner)
    return answer_question_rule_based(query, owner)


# --- Agentic actions: create an owner, add a pet, schedule a task ------------
# Shared by both the Gemini function-calling path and the rule-based
# structured-command path, so behavior (and guardrails) stay identical
# regardless of which mode is active.

def _do_create_owner(session_state, name, email, phone) -> str:
    if session_state.get("owner") is not None:
        return f"You already have an owner profile set up for {session_state.owner.name}."
    session_state.owner = Owner(name=str(name), email=str(email), phone=str(phone))
    logger.info("Owner created via chat: name=%s", name)
    return f"Done — I've set up an owner profile for {name}."


def _do_add_pet(session_state, name, species, breed, age, weight) -> str:
    owner = session_state.get("owner")
    if owner is None:
        return "You'll need an owner profile first — tell me a name, email, and phone and I'll set that up."
    try:
        age_int = int(float(age))
        weight_float = float(weight)
    except (TypeError, ValueError):
        return f"I couldn't add {name} — age and weight both need to be numbers."
    pet = Pet(name=str(name), species=str(species).strip().title(), breed=str(breed),
              age=age_int, weight=weight_float)
    owner.add_pet(pet)
    logger.info("Pet added via chat: name=%s species=%s breed=%s", name, species, breed)
    return f"Done — I've added {name}, a {age_int}-year-old {species} ({breed}), to your pets."


def _do_add_task(session_state, pet_name, description, time, frequency) -> str:
    owner = session_state.get("owner")
    if owner is None or not owner.pets:
        return "You'll need at least one pet registered before I can schedule a task for them."
    pet = next((p for p in owner.pets if p.name.lower() == str(pet_name).strip().lower()), None)
    if pet is None:
        names = ", ".join(p.name for p in owner.pets)
        return f"I don't see a pet named {pet_name} — your pets are: {names}."
    try:
        datetime.strptime(str(time), "%I:%M %p")
    except ValueError:
        logger.warning("Chat rejected task with invalid time format: %r", time)
        return f"'{time}' isn't a valid time — please use the format 'H:MM AM/PM', e.g. '8:00 AM'."
    freq = str(frequency).strip().lower()
    if freq not in {"daily", "weekly", "monthly"}:
        freq = "daily"
    pet.add_task(Task(description=str(description), time=str(time), frequency=freq))
    logger.info("Task added via chat: pet=%s desc=%s time=%s freq=%s", pet_name, description, time, freq)
    return f"Done — I've scheduled '{description}' for {pet.name} at {time} ({freq})."


_ACTION_HANDLERS = {
    "create_owner": _do_create_owner,
    "add_pet": _do_add_pet,
    "add_task": _do_add_task,
}


def _gemini_action_tools(owner: Optional[Owner]):
    """Only expose actions that are actually valid given current state, so
    Gemini can't be tempted into calling e.g. add_task before any pet exists.
    """
    from google.genai import types

    declarations = []
    if owner is None:
        declarations.append(types.FunctionDeclaration(
            name="create_owner",
            description="Create the owner profile. Only call this if no owner exists yet.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "email": {"type": "STRING"},
                    "phone": {"type": "STRING"},
                },
                "required": ["name", "email", "phone"],
            },
        ))
        return [types.Tool(function_declarations=declarations)]

    declarations.append(types.FunctionDeclaration(
        name="add_pet",
        description="Register a new pet under the current owner.",
        parameters={
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING"},
                "species": {"type": "STRING", "description": "e.g. Dog, Cat, Bird"},
                "breed": {"type": "STRING"},
                "age": {"type": "INTEGER", "description": "Age in years"},
                "weight": {"type": "NUMBER", "description": "Weight in pounds"},
            },
            "required": ["name", "species", "breed", "age", "weight"],
        },
    ))
    if owner.pets:
        declarations.append(types.FunctionDeclaration(
            name="add_task",
            description="Schedule a new care task for an existing pet.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "pet_name": {"type": "STRING"},
                    "description": {"type": "STRING", "description": "e.g. Morning walk, Feed breakfast"},
                    "time": {"type": "STRING", "description": "Time in 'H:MM AM/PM' format, e.g. '8:00 AM'"},
                    "frequency": {"type": "STRING", "description": "daily, weekly, or monthly"},
                },
                "required": ["pet_name", "description", "time", "frequency"],
            },
        ))
    return [types.Tool(function_declarations=declarations)]


_KV_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|(\S+))')


def _parse_kv(text: str) -> dict:
    return {key.lower(): (quoted if quoted else bare) for key, quoted, bare in _KV_RE.findall(text)}


def _try_rule_based_action(query: str, session_state) -> Optional[str]:
    """Structured command syntax for the offline mode, e.g.:
    'add pet name=Rex species=Dog breed=Labrador age=3 weight=60'
    'add task pet=Mochi description="Morning walk" time="8:00 AM" frequency=daily'
    'create owner name=Sarah email=sarah@email.com phone=555-1234'

    Returns a confirmation/guidance string if this looks like an action
    request, or None if it doesn't match any action trigger at all (in which
    case the caller should treat it as an ordinary question).
    """
    lower = query.strip().lower()
    kv = _parse_kv(query)

    if lower.startswith(("add pet", "create pet", "register pet", "new pet")):
        required = ["name", "species", "breed", "age", "weight"]
        missing = [f for f in required if f not in kv]
        if missing:
            return (
                f"I can add a pet — I just need: {', '.join(missing)}. Try: "
                '"add pet name=Rex species=Dog breed=Labrador age=3 weight=60"'
            )
        return _do_add_pet(session_state, kv["name"], kv["species"], kv["breed"], kv["age"], kv["weight"])

    if lower.startswith(("add task", "schedule task", "create task", "new task")):
        required = ["pet", "description", "time", "frequency"]
        missing = [f for f in required if f not in kv]
        if missing:
            return (
                f"I can schedule a task — I just need: {', '.join(missing)}. Try: "
                'add task pet=Mochi description="Morning walk" time="8:00 AM" frequency=daily'
            )
        return _do_add_task(session_state, kv["pet"], kv["description"], kv["time"], kv["frequency"])

    if lower.startswith(("create owner", "add owner", "register owner", "new owner")):
        required = ["name", "email", "phone"]
        missing = [f for f in required if f not in kv]
        if missing:
            return (
                f"I can set up an owner profile — I just need: {', '.join(missing)}. Try: "
                '"create owner name=Sarah email=sarah@email.com phone=555-1234"'
            )
        return _do_create_owner(session_state, kv["name"], kv["email"], kv["phone"])

    return None


def _gemini_turn(query: str, session_state) -> str:
    """One Gemini call that can either perform an action (function calling) or
    answer a question via RAG — falls back to the rule-based path (including
    its own structured-command action parsing) on any failure.
    """
    owner = session_state.get("owner")
    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai not installed; falling back to rule-based answer/action")
        action = _try_rule_based_action(query, session_state)
        return action if action is not None else answer_question_rule_based(query, owner)

    chunks = build_knowledge_base(owner)
    relevant = retrieve_relevant_chunks(query, chunks, top_k=6)
    context = "\n\n".join(chunk for chunk, _score in relevant)

    try:
        from google.genai import types

        client = genai.Client(api_key=_get_gemini_api_key())
        config = types.GenerateContentConfig(tools=_gemini_action_tools(owner))
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{GEMINI_AGENT_SYSTEM_PROMPT.format(context=context)}\n\nUser: {query}",
            config=config,
        )
        part = response.candidates[0].content.parts[0]

        if part.function_call is not None:
            fn_name = part.function_call.name
            fn_args = dict(part.function_call.args or {})
            handler = _ACTION_HANDLERS.get(fn_name)
            if handler is None:
                logger.warning("Gemini requested an unrecognized function: %s", fn_name)
                return answer_question_rule_based(query, owner)
            logger.info("Gemini action: %s(%r)", fn_name, fn_args)
            return handler(session_state, **fn_args)

        text = (part.text or "").strip()
        return text or "I didn't get a usable response back — please try again."
    except Exception:
        logger.exception("Gemini request failed for query=%r; falling back to rule-based answer/action", query)
        action = _try_rule_based_action(query, session_state)
        return action if action is not None else answer_question_rule_based(query, owner)


def handle_chat_message(query: str, session_state) -> str:
    """Main entry point for the chat UI: performs an action if requested
    (create an owner, add a pet, schedule a task), otherwise answers the
    question — via Gemini if configured, rule-based otherwise.
    """
    if _get_gemini_api_key():
        return _gemini_turn(query, session_state)

    action_result = _try_rule_based_action(query, session_state)
    if action_result is not None:
        return action_result
    return answer_question_rule_based(query, session_state.get("owner"))


def render_ai_assistant() -> None:
    """Render the AI assistant widget. Call near the top of the page.

    Placed inline (not floating) so it always renders reliably — Streamlit
    wraps blocks in animated wrapper divs that break plain CSS `position:
    fixed`, which silently clips floating elements instead of just
    repositioning them. Being first on the page means no scrolling is ever
    needed to reach it or see its latest answer. Its open/closed state lives
    in session_state and is only cleared by the explicit close ("✕") button,
    so asking questions never dismisses it.
    """
    if "ai_chat_history" not in st.session_state:
        st.session_state.ai_chat_history = []
    if "ai_chat_open" not in st.session_state:
        st.session_state.ai_chat_open = False

    if st.button("🤖 AI Assistant", key="ai_launcher_btn", help="Open AI Assistant"):
        st.session_state.ai_chat_open = True

    if not st.session_state.ai_chat_open:
        return

    with st.container(border=True):
        title_col, close_col = st.columns([5, 1])
        with title_col:
            st.markdown("**🤖 AI Assistant**")
        with close_col:
            if st.button("✕", key="ai_close_btn", help="Close"):
                st.session_state.ai_chat_open = False
                st.rerun()

        mode_caption = "Powered by Gemini" if _get_gemini_api_key() else "Rule-based — no API key needed"
        st.caption(f"Ask about your pets/tasks, or tell me to add a pet, task, or owner. ({mode_caption}.)")

        history = st.session_state.ai_chat_history
        # Keep the most recent exchange directly visible (no scrolling needed);
        # older turns are tucked into a small scrollable pane above it.
        older, recent = (history[:-2], history[-2:]) if len(history) > 2 else ([], history)

        if older:
            with st.container(height=140, border=True):
                for msg in older:
                    with st.chat_message(msg["role"]):
                        st.markdown(msg["content"])

        for msg in recent:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        user_input = st.chat_input("Ask, or tell me to add a pet/task/owner...", key="ai_assistant_input")
        if user_input:
            st.session_state.ai_chat_history.append({"role": "user", "content": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)

            logger.info("AI assistant message: %r", user_input)
            try:
                answer = handle_chat_message(user_input, st.session_state)
            except Exception:
                logger.exception("AI assistant failed to handle message: %r", user_input)
                answer = "Sorry, something went wrong with that — try rephrasing it."

            with st.chat_message("assistant"):
                st.markdown(answer)
            st.session_state.ai_chat_history.append({"role": "assistant", "content": answer})
