# Roadmap: AcademicLink Commercial Version

## Phase 1: Bug Fixing & Technical Debt ✅
- [x] **Fix Availability Logic:** Transition from hardcoded settings (`working_hour_start/end`) to using the `AvailabilitySlot` model in the database.
- [x] **Booking Conflicts:** Prevent creating `PENDING` bookings if the time is already occupied by another `PENDING` or `CONFIRMED` booking.
- [x] **Code Cleanup:** Remove or refactor unused parts of the logic.

## Phase 2: Core Commercial Features ✅
- [x] **Flexible Lesson Duration:** Allow tutors to set different durations (45, 60, 90 min) and add "buffer times" between lessons.
- [x] **Slot Management:** Full CRUD for availability slots via the Telegram bot (Settings → Мои слоты).
- [x] **Mass Mailings:** Feature for tutors to send messages to all their students via the bot (📢 Рассылка).

## Phase 3: Advanced Features ✅
- [x] **Smart Reminders:** Added 24-hour and pre-lesson reminders with opt-in/opt-out toggle for both tutors and students.
- [ ] **Google Calendar Integration:** Syncing bookings with the tutor's personal calendar (planned).
- [ ] **Mini-Landing Page:** Enhancing the web interface into a proper booking page for students (planned).

## Features NOT Planned (Removed)
- Financial accounting, payment tracking, and subscription management (keeping it simple for now).
