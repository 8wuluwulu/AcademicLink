# AcademicLink — Context and Current Status

> **Note for the AI Assistant:** Read this file to quickly align on the project state, changes made, and configuration details.

---

## 🚀 What Was Completed

1. **FastAPI Route Resolution Order Fixed**:
   * Moved the `GET /api/v1/tutors/by-student` route above `GET /api/v1/tutors/{tutor_id}` inside [tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py).
   * Added the missing `Student` database model import inside [tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py) to enable database query joins.
   * This successfully resolves the `422 Unprocessable Content` error where FastAPI was matching the literal string `"by-student"` to the integer `{tutor_id}` path parameter.

2. **Student Bot Registration & Multi-Tutor Support**:
   * Modified `start_student_registration` in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py) to check for student registration specifically for the current `tutor_id` instead of searching globally.
   * If a student is already registered with another tutor, the bot now automatically creates a new `Student` record for the new tutor, copying their contact details (name and phone) instantly to ensure they are linked to both tutors.
   - Updated automatic student linking by username inside `_send_dashboard` to link all records matching the username instead of just one.

3. **Direct WebApp Sign-Up (Removed Bot Selection Dialog)**:
   * Updated `build_student_menu` and `cmd_book_select_tutor` in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py) to make the "📅 Записаться" button always open the WebApp directly (pointing to the first tutor) rather than sending inline selection buttons inside the chat.
   * The student can switch tutors directly within the WebApp's dropdown header.

4. **Dynamic In-Place WebApp Tutor Switching**:
   * Refactored [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) to switch tutors in-place dynamically (using `window.history.pushState` and loading details/services via JS) rather than triggering full page reloads (`window.location.href`).
   * This prevents losing the Telegram WebApp context and session authorization hash inside the Telegram WebApp frame.
   * Personalized brand accent colors are applied dynamically to the web UI when switching tutors on the fly.

5. **Polished Dropdown Selection Style**:
   * Redesigned the tutor select box in [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) with clean borders, modern shadows, hover transitions, and a customized CSS-drawn chevron arrow (`▼`).
   * Removed the emoji sticker (`👨‍🏫`) prefix from select option templates.

6. **Form Submission & Validation Error Fixes**:
   * Added the missing `<input type="tel" id="phone">` field in [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) which was causing a JS TypeError blocking submission.
   * Normalized phone number inputs on submission (stripping formatting and converting Russian `8...` to `+7...` to match API regex requirements).
   * Formatted FastAPI validation errors properly in [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) to show readable strings (e.g. `phone: Номер телефона должен быть...`) instead of `[object Object]`.

7. **Verified Integrity**:
   * All 37 integration and backend tests pass successfully (`pytest` status: green).

---

## 📂 Key File Locations

* **[landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html)**: Personal student booking page. Contains HTML/CSS/JS with SBP modal payment logic and dynamic tutor switching.
* **[tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py)**: Router file containing tutor endpoints.
* **[handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py)**: The Telegram bot handlers containing deep-linking registration flow and student keyboards.
* **[test_api_booking.py](file:///D:/Portfolio/Academic/AcademicLink/tests/test_api_booking.py)**: Integration tests verifying booking creating API and the new `/tutors/by-student` route.

---

## 🔮 Next Steps & Ideas for the Next Session

* Build a mechanism in the Telegram bot to notify tutors immediately when a student schedules a booking with them.
* Add automated background jobs for lesson reminders (e.g. sending a Telegram message to the student 1 hour before the lesson starts).
