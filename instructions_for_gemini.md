# AcademicLink — Context and Current Status

> **Note for the AI Assistant:** Read this file to quickly align on the project state, changes made, and configuration details.

---

## 🚀 What Was Completed

1. **FastAPI Route Resolution Order Fixed**:
   * Moved the `GET /api/v1/tutors/by-student` route above `GET /api/v1/tutors/{tutor_id}` inside [tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py).
   * Added the missing `Student` database model import inside [tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py) to enable database query joins.
   * This successfully resolves the `422 Unprocessable Content` error where FastAPI was matching the literal string `"by-student"` to the integer `{tutor_id}` path parameter.

2. **Many-to-Many Student-Tutor Architecture**:
   * Designed and implemented the M2M link model `StudentTutorLink` inside [models.py](file:///D:/Portfolio/Academic/AcademicLink/app/db/models.py) containing tutor-specific details (`prepaid_balance`, `notes`, and `is_active`).
   * Cleaned up the `Student` model to represent global student identity (with unique phone and telegram_id fields).
   * Developed a robust database migration script inside [database.py](file:///D:/Portfolio/Academic/AcademicLink/app/db/database.py) that automatically detects older schema states, deduplicates existing student records, moves balances and notes to the link table, redirects bookings, and drops deprecated columns.
   * Refactored API routers ([tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py)), booking services ([booking_service.py](file:///D:/Portfolio/Academic/AcademicLink/app/services/booking_service.py)), and all bot handlers ([handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py)) to join the link table and handle registrations/deletions seamlessly without duplicating student data.
   * Updated the test suite fixtures and test files to seed `StudentTutorLink` records.

3. **Student Bot Registration & Multi-Tutor Support**:
   * Modified `start_student_registration` in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py) to check for student registration specifically for the current `tutor_id` instead of searching globally.
   * If a student is already registered with another tutor, the bot now automatically creates a new association link `StudentTutorLink` for the new tutor, copying their contact details (name and phone) instantly to ensure they are linked to both tutors.
   * Updated automatic student linking by username inside `_send_dashboard` to link all records matching the username instead of just one.

4. **Direct WebApp Sign-Up (Removed Bot Selection Dialog)**:
   * Updated `build_student_menu` and `cmd_book_select_tutor` in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py) to make the "📅 Записаться" button always open the WebApp directly (pointing to the first tutor) rather than sending inline selection buttons inside the chat.
   * The student can switch tutors directly within the WebApp's dropdown header.

5. **Dynamic In-Place WebApp Tutor Switching**:
   * Refactored [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) to switch tutors in-place dynamically (using `window.history.pushState` and loading details/services via JS) rather than triggering full page reloads (`window.location.href`).
   * This prevents losing the Telegram WebApp context and session authorization hash inside the Telegram WebApp frame.
   * Personalized brand accent colors are applied dynamically to the web UI when switching tutors on the fly.

6. **Polished Dropdown Selection Style**:
   * Redesigned the tutor select box in [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) with clean borders, modern shadows, hover transitions, and a customized CSS-drawn chevron arrow (`▼`).
   * Removed the emoji sticker (`👨‍🏫`) prefix from select option templates.

7. **Form Submission & Validation Error Fixes**:
   * Added the missing `<input type="tel" id="phone">` field in [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) which was causing a JS TypeError blocking submission.
   * Normalized phone number inputs on submission (stripping formatting and converting Russian `8...` to `+7...` to match API regex requirements).
   * Formatted FastAPI validation errors properly in [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) to show readable strings (e.g. `phone: Номер телефона должен быть...`) instead of `[object Object]`.

8. **Verified Integrity**:
   * All 37 integration and backend tests pass successfully (`pytest` status: green).

---

## 📂 Key File Locations

* **[landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html)**: Personal student booking page. Contains HTML/CSS/JS with SBP modal payment logic and dynamic tutor switching.
* **[tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py)**: Router file containing tutor endpoints.
* **[handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py)**: The Telegram bot handlers containing deep-linking registration flow and student keyboards.
* **[test_api_booking.py](file:///D:/Portfolio/Academic/AcademicLink/tests/test_api_booking.py)**: Integration tests verifying booking creating API and the new `/tutors/by-student` route.

---

## 🔮 Next Steps & Ideas for the Next Session

* **Filter Out Past Bookings (Schedule Bug)**:
  * Currently, past lessons are not removed from the active schedule lists, causing them to accumulate under `📅 Расписание` / `🟡 Новые заявки` (tutors) and `🗂 Мои записи` (students).
  * Filter `Booking.appointment_time` to exclude past lessons (e.g. `Booking.appointment_time >= now_utc`).

* **Student Cancellation & Rescheduling with Tutor Notifications**:
  * Add inline keyboard buttons (`❌ Отменить` and `🔄 Перенести`) to each booking in the student's `🗂 Мои записи` list.
  * Update `TutorCallbackMiddleware` in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py) to allow student callbacks starting with `student_` prefix to bypass tutor validation.
  * Implement confirmation/safety buffer checks (`settings.cancel_safety_hours`) for cancellations.
  * Send immediate Telegram notifications to tutors (`booking.tutor.tg_id`) with details whenever a student cancels or reschedules.

* **Student Reminder Toggle**:
  * Add an inline toggle button (`🔔 Напоминания: Вкл` / `🔕 Напоминания: Выкл`) on the student welcome dashboard `_send_dashboard` to let students manage their reminder preference (`Student.wants_reminders`).
