import logging
from datetime import datetime

import streamlit as st
from pawpal_system import Owner, Pet, Task, Scheduler
from ai_assistant import render_ai_assistant

logging.basicConfig(
    filename="pawpal.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")
st.title("🐾 PawPal+")

# --- Session state init ---
if "owner" not in st.session_state:
    st.session_state.owner = None

# ── AI Assistant (pinned at the top so it's always reachable, no scrolling) ───
render_ai_assistant()
st.divider()

# ── Owner Setup ────────────────────────────────────────────────────────────────
st.subheader("Owner")

col1, col2, col3 = st.columns(3)
with col1:
    owner_name = st.text_input("Name", value="Jordan")
with col2:
    owner_email = st.text_input("Email", value="jordan@email.com")
with col3:
    owner_phone = st.text_input("Phone", value="555-0100")

if st.button("Create Owner"):
    if st.session_state.owner is None:
        st.session_state.owner = Owner(name=owner_name, email=owner_email, phone=owner_phone)
        logger.info("Owner created: name=%s", owner_name)
        st.success(f"Owner '{owner_name}' created!")
    else:
        st.info(f"Owner '{st.session_state.owner.name}' already exists.")

if st.session_state.owner:
    st.caption(st.session_state.owner.get_contact_info())

st.divider()

# ── Add a Pet ──────────────────────────────────────────────────────────────────
st.subheader("Add a Pet")

if st.session_state.owner is None:
    st.warning("Create an owner first.")
else:
    col1, col2, col3 = st.columns(3)
    with col1:
        pet_name = st.text_input("Pet name", value="Mochi")
    with col2:
        species = st.selectbox("Species", ["Dog", "Cat", "Bird", "Other"])
    with col3:
        breed = st.text_input("Breed", value="Shiba Inu")

    col4, col5 = st.columns(2)
    with col4:
        age = st.number_input("Age (years)", min_value=0, max_value=30, value=2)
    with col5:
        weight = st.number_input("Weight (lbs)", min_value=0.1, max_value=300.0, value=15.0)

    if st.button("Add Pet"):
        new_pet = Pet(name=pet_name, species=species, breed=breed, age=int(age), weight=float(weight))
        st.session_state.owner.add_pet(new_pet)
        logger.info("Pet added: name=%s species=%s breed=%s", pet_name, species, breed)
        st.success(f"{pet_name} added!")

    if st.session_state.owner.pets:
        st.write("Registered pets:")
        st.table([{"Name": p.name, "Species": p.species, "Breed": p.breed,
                   "Age": p.age, "Weight (lbs)": p.weight}
                  for p in st.session_state.owner.pets])

st.divider()

# ── Schedule a Task ────────────────────────────────────────────────────────────
st.subheader("Schedule a Task")

if st.session_state.owner is None or not st.session_state.owner.pets:
    st.warning("Add an owner and at least one pet first.")
else:
    pet_names = [p.name for p in st.session_state.owner.pets]

    col1, col2 = st.columns(2)
    with col1:
        selected_pet = st.selectbox("Assign to pet", pet_names)
    with col2:
        task_desc = st.text_input("Task description", value="Morning walk")

    col3, col4 = st.columns(2)
    with col3:
        task_time = st.text_input("Time", value="8:00 AM")
    with col4:
        frequency = st.selectbox("Frequency", ["daily", "weekly", "monthly"])

    if st.button("Add Task"):
        try:
            datetime.strptime(task_time, "%I:%M %p")
        except ValueError:
            logger.warning("Rejected task with invalid time format: %r", task_time)
            st.error(f"'{task_time}' isn't a valid time. Use the format 'H:MM AM/PM', e.g. '8:00 AM'.")
        else:
            target_pet = next(p for p in st.session_state.owner.pets if p.name == selected_pet)
            new_task = Task(description=task_desc, time=task_time, frequency=frequency)
            target_pet.add_task(new_task)
            logger.info(
                "Task added: pet=%s desc=%s time=%s freq=%s",
                selected_pet, task_desc, task_time, frequency,
            )
            st.success(f"Task '{task_desc}' added to {selected_pet}!")

st.divider()

# ── Today's Schedule ───────────────────────────────────────────────────────────
st.subheader("Today's Schedule")

if st.session_state.owner is None or not st.session_state.owner.pets:
    st.info("No schedule yet. Add pets and tasks above.")
else:
    scheduler = Scheduler(st.session_state.owner)

    conflicts = scheduler.detect_conflicts()
    if conflicts:
        for warning in conflicts:
            st.warning(warning)
    else:
        st.success("No scheduling conflicts detected.")

    for pet in st.session_state.owner.pets:
        st.markdown(f"**{pet.name}** ({pet.breed})")
        pending = scheduler.sort_by_time(
            scheduler.filter_tasks(completed=False, pet_names=[pet.name])
        )
        if not pending:
            st.success(f"{pet.name} has no pending tasks. All caught up!")
            continue

        st.table([
            {
                "Time": task.time,
                "Task": task.description,
                "Frequency": task.frequency.title(),
                "Date": task.date or "—",
            }
            for task in pending
        ])

        col1, col2 = st.columns([3, 1])
        with col1:
            task_to_complete = st.selectbox(
                "Mark a task complete",
                [task.description for task in pending],
                key=f"select_{pet.name}",
            )
        with col2:
            st.write("")
            if st.button("Mark Done", key=f"done_{pet.name}"):
                scheduler.complete_task(pet.name, task_to_complete)
                logger.info("Task marked complete: pet=%s task=%s", pet.name, task_to_complete)
                st.success(f"Marked '{task_to_complete}' complete for {pet.name}!")
                st.rerun()

    st.divider()
    pending_count = len(scheduler.get_all_pending_tasks())
    st.metric("Total pending tasks", pending_count)
