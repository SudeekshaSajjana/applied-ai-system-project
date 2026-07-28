"""Rule-based AI chatbot for PawPal+.

No external LLM or API key required. Answers are generated entirely from
the live owner/pet/task data already held in Streamlit session state:
the bot detects which pet (if any) and which intent a question is about,
then composes the answer directly from that data. A lightweight keyword
retrieval step is kept as a fallback for questions that don't match a
known intent, so the bot still returns something grounded in the app's
data rather than a generic "I don't know."
"""

import logging
import re
from typing import List, Optional

import streamlit as st

from pawpal_system import Owner, Scheduler

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


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
        return ["No owner has been created yet. No pets or tasks exist in the app yet."]

    chunks = [f"Owner contact info: {owner.get_contact_info()}."]

    if not owner.pets:
        chunks.append("The owner has not registered any pets yet.")
        return chunks

    scheduler = Scheduler(owner)

    for pet in owner.pets:
        chunks.append(f"Pet profile — {pet.get_profile()}")
        if not pet.tasks:
            chunks.append(f"{pet.name} has no tasks scheduled.")
            continue
        task_lines = [f"Tasks for {pet.name}:"] + [f"- {t.get_summary()}" for t in pet.tasks]
        chunks.append("\n".join(task_lines))
        chunks.append(f"{pet.name} has {len(pet.get_pending_tasks())} pending task(s).")

    conflicts = scheduler.detect_conflicts()
    chunks.append("Scheduling conflicts:\n" + "\n".join(conflicts) if conflicts
                   else "There are no scheduling conflicts across any pet.")
    chunks.append(scheduler.get_daily_schedule())
    chunks.append(f"Total pending tasks across all pets: {len(scheduler.get_all_pending_tasks())}.")
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


def answer_question(query: str, owner: Optional[Owner]) -> str:
    """Answer a question using rule-based intent matching over live app data."""
    tokens = set(_tokenize(query))

    if tokens & GREETING_WORDS:
        return (
            "Hi! I'm the PawPal+ assistant. Ask me things like:\n"
            "- \"What tasks does Mochi have?\"\n"
            "- \"Are there any scheduling conflicts?\"\n"
            "- \"What's today's schedule?\"\n"
            "- \"How many pending tasks does Buddy have?\""
        )

    if tokens & HELP_WORDS:
        return (
            "I can answer questions about your owner info, pets, tasks, pending/completed "
            "status, scheduling conflicts, and today's schedule — all based on what's "
            "currently entered in the app."
        )

    if owner is None:
        return "No owner has been created yet — add one in the form above and I can help from there."

    scheduler = Scheduler(owner)

    if tokens & OWNER_WORDS:
        return f"Owner info: {owner.get_contact_info()}"

    if tokens & PET_LIST_WORDS:
        if not owner.pets:
            return "No pets have been registered yet."
        lines = [f"You have {len(owner.pets)} pet(s):"]
        lines += [f"- {p.get_profile()}" for p in owner.pets]
        return "\n".join(lines)

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
                return f"{pet.name} has no pending tasks. All caught up!"
            return f"{pet.name}'s pending tasks:\n" + "\n".join(f"- {t.get_summary()}" for t in pending)

        if tokens & COMPLETED_WORDS:
            done = [t for t in pet.tasks if t.completed]
            if not done:
                return f"{pet.name} has no completed tasks yet."
            return f"{pet.name}'s completed tasks:\n" + "\n".join(f"- {t.get_summary()}" for t in done)

        if not pet.tasks:
            return f"{pet.name} has no tasks scheduled."
        return f"{pet.name}'s tasks:\n" + "\n".join(f"- {t.get_summary()}" for t in pet.tasks)

    if tokens & PENDING_WORDS:
        total = len(scheduler.get_all_pending_tasks())
        return f"There are {total} pending task(s) across all pets."

    if tokens & TASK_WORDS:
        all_tasks = owner.get_all_tasks()
        if not all_tasks:
            return "No tasks have been added for any pet yet."
        lines = ["All tasks:"]
        for p in owner.pets:
            for t in p.tasks:
                lines.append(f"- {p.name}: {t.get_summary()}")
        return "\n".join(lines)

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

        st.caption("Ask about your pets, tasks, or today's schedule. (No API key needed.)")

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

        user_input = st.chat_input("Ask the AI assistant...", key="ai_assistant_input")
        if user_input:
            st.session_state.ai_chat_history.append({"role": "user", "content": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)

            logger.info("AI assistant question: %r", user_input)
            try:
                answer = answer_question(user_input, st.session_state.get("owner"))
            except Exception:
                logger.exception("AI assistant failed to answer: %r", user_input)
                answer = "Sorry, something went wrong answering that — try rephrasing your question."

            with st.chat_message("assistant"):
                st.markdown(answer)
            st.session_state.ai_chat_history.append({"role": "assistant", "content": answer})
