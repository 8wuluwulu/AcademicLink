# AcademicLink — Context and Current Status

> **Note for the AI Assistant:** Read this file to quickly align on the project state, changes made, and configuration details.

---

## 🚀 What Was Completed

1. **FastAPI Route Resolution Order Fixed**:
   * Moved the `GET /api/v1/tutors/by-student` route above `GET /api/v1/tutors/{tutor_id}` inside [tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py).
   * Added the missing `Student` database model import inside [tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py) to enable database query joins.

2. **Many-to-Many Student-Tutor Architecture**:
   * Designed and implemented the M2M link model `StudentTutorLink` inside [models.py](file:///D:/Portfolio/Academic/AcademicLink/app/db/models.py) containing tutor-specific details (`prepaid_balance`, `notes`, and `is_active`).
   * Cleaned up the `Student` model to represent global student identity.
   * Developed a database migration script inside [database.py](file:///D:/Portfolio/Academic/AcademicLink/app/db/database.py) that deduplicates student records and moves balances/notes to the link table.

3. **Student Bot Registration & Multi-Tutor Support**:
   * Modified student registration in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py) to associate student accounts with new tutors dynamically without duplicating student contact details.

4. **Dynamic In-Place WebApp Tutor Switching**:
   * Refactored [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html) to switch tutors in-place dynamically using `window.history.pushState` and loading details/services via JS, avoiding full page reloads to maintain Telegram context.

5. **Student settings & Reminder Toggle**:
   * Added the `⚙️ Настройки` button to the student's reply menu keyboard layout in [handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py#L128).
   * Clicking this button now invokes [cmd_settings](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py#L1323) which recognizes them as a student and shows their profile info along with an inline toggle button for reminders (`🔔 Напоминания: Вкл / Выкл`).
   * Spamming `/start` as a student is fully safe, clearing FSM states and reloading the dashboard.

6. **Simplified SBP Payment Details**:
   * Removed Tinkoff payment link (`sbp_link`) and QR code (`sbp_qr_url`) fields from the tutor's SBP settings in the bot.
   * Completely removed the QR code container (`modal-qr-container`) and generator logic from the WebApp [landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html). It now shows only the recipient's phone and bank name.

7. **Tutor Settings Cleanup**:
   * Removed the pause button (`🔴 Пауза` / `🟢 Старт`) and status info from the settings menu.
   * Removed Zoom/Meet conference link (`meeting_link`) configurations and handlers.
   * Removed the direct landing page link, keeping only the Telegram invitation ref link.

8. **Polished Service & Absence Deletion**:
   * Replaced raw text commands `/del_service_{id}` in the service list with beautiful inline buttons `🗑 Удалить «{service.name}»` mapping to callback handler [cb_del_service](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py#L2920).
   * Replaced raw text commands `/del_absence_{id}` in the absence list with clean inline buttons `🗑 Удалить: {date}` mapping to callback handler [cb_del_absence](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py#L1773).

9. **Tutor Name in Cancellation Alerts**:
   * Updated tutor cancellation alerts (quick block, absences, manual cancellation, payment rejection) to load and print the tutor's name (`{tutor_name}`) to the student.

10. **Broadcast Sender Info & Direct Tutor Link**:
    * Prefixed broadcast messages sent to students with the tutor's name (e.g., `📢 Сообщение от преподавателя {tutor_name}:\n\n{text}`).
    * Enhanced the preview message shown to tutors to show exactly how the prefix will look and list that the `💬 Написать репетитору` button will be appended.
    * Added an inline keyboard to every broadcast message sent to students containing a single button: `💬 Написать репетитору` which opens a direct Telegram chat link (`https://t.me/{username}` or fallback to `tg://user?id={tg_id}`).
    * Simplified the mailing result screen for the tutor to display a clean success/error status instead of a detailed recipient breakdown.

11. **Google Calendar Sync & Session Refresh Bug Fixed**:
    * Added `await session.flush()` before `await session.refresh(booking)` inside `delete_calendar_event` in [google_calendar_service.py](file:///D:/Portfolio/Academic/AcademicLink/app/services/google_calendar_service.py#L237).
    * This prevents in-memory modifications (like `booking.status = BookingStatus.CANCELLED`) from being lost/overwritten when SQLAlchemy reloads data from the database.

12. **Tutor Reschedule Rejection Callback Mismatch Resolved**:
    * Updated the decorator for [cb_tutor_resched_reject](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py#L3262) to use `@router.callback_query(F.data.startswith("tr_r:"))` matching the short callback prefix (shortened to stay within the 64-byte Telegram limit).
    * Updated the mock data in [test_student_features.py](file:///D:/Portfolio/Academic/AcademicLink/tests/test_student_features.py#L364) to match this short prefix.

13. **Verified Integrity**:
    * Added integration tests `test_cb_student_cancel_confirm` and `test_cb_tutor_cancel_confirm` in [test_student_features.py](file:///D:/Portfolio/Academic/AcademicLink/tests/test_student_features.py#L456) to verify cancellation flows.
    * All **52 integration and backend tests** pass successfully (`pytest` status: green).

---

## 📂 Key File Locations

* **[landing.html](file:///D:/Portfolio/Academic/AcademicLink/landing.html)**: Personal student booking page. Contains HTML/CSS/JS with SBP modal payment logic and dynamic tutor switching.
* **[tutor.py](file:///D:/Portfolio/Academic/AcademicLink/app/api/tutor.py)**: Router file containing tutor endpoints.
* **[handlers.py](file:///D:/Portfolio/Academic/AcademicLink/app/bot/handlers.py)**: Bot handlers containing FSM, keyboards, and message routers.
* **[models.py](file:///D:/Portfolio/Academic/AcademicLink/app/db/models.py)**: SQLModel structures.
* **[google_calendar_service.py](file:///D:/Portfolio/Academic/AcademicLink/app/services/google_calendar_service.py)**: Google Calendar API sync helper.
* **[how_to_test_all_scenarios.md](file:///C:/Users/Admin/.gemini/antigravity-cli/brain/0b475b14-a07d-449a-936d-f64f86114d96/how_to_test_all_scenarios.md)**: Detailed checklist of all manual test scenarios for tutors and students in Russian.

---

* **All Tasks Completed**:
  * Deactivated student lockout (restricted bot access for students with 0 active links) is implemented.
  * Archived student restoration via phone search (`🔍 Найти по номеру` -> `🟢 Восстановить ученика`) is implemented.
  * Same-time rescheduling prevention check (ensuring a booking cannot be rescheduled to its current time) is implemented and verified.
  * Student-proposed reschedule flow: instead of immediate rescheduling, a request is sent to the tutor's Telegram chat containing inline buttons `✅ Подтвердить`, `❌ Отклонить перенос`, and `💬 Связаться с учеником`.
  * Tutor registration FSM flow: during onboarding, new tutors are prompted to enter their own full name (FIO) instead of using their Telegram profile name automatically.
  * Google Calendar sync refresh bug fixed.
  * Shortened callback prefix for reschedule rejection implemented.
  * Integration tests added and verified.
  * **Tutor subscriptions listing inside `/admin` dashboard**: shows copyable tutor IDs, display names, and color-coded status info with dates in MSK timezone.
  * **Strict subscription lifecycle blocks**: expired/revoked tutors are blocked from P2P confirmation/rejection callbacks, main menu FSM dashboard is redirected to block page, and `ReplyKeyboardRemove` is used to instantly clear tutor menu keyboards upon revocation.
  * **Real-time schedule filtering**: removed the 15-minute delay/caching for past bookings, so they disappear immediately from the tutor's active schedule and the student's active booking list at the exact start minute of the lesson.
  * **Automated Bot Metadata**: sets bot description (pre-start screen) and short description programmatically on startup.
  * **VPS Deployment**: Deployed on Beget VPS (IP: `212.67.11.175`) with PostgreSQL database, systemd daemon service, Nginx reverse proxy, and registered domain `academiclink.ru` (delegation pending for Certbot SSL setup).
  * **Contact Support System**: Implemented a bilateral support communication channel for both tutors and students using `SupportStates` and `settings.admin_tg_id`. Tutors with expired subscriptions bypass middleware checks to access support.

