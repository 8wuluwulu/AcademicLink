# Roadmap: AcademicLink Commercial Version

## Phase 1: Bug Fixing & Technical Debt (Current)
- [ ] **Fix Availability Logic:** Transition from hardcoded settings (`working_hour_start/end`) to using the `AvailabilitySlot` model in the database.
- [ ] **Booking Conflicts:** Prevent creating `PENDING` bookings if the time is already occupied by another `PENDING` or `CONFIRMED` booking.
- [ ] **Code Cleanup:** Remove or refactor unused parts of the logic.

## Phase 2: Core Commercial Features
- [ ] **Flexible Lesson Duration:** Allow tutors to set different durations (45, 60, 90 min) and add "buffer times" between lessons.
- [ ] **Mass Mailings:** Feature for tutors to send messages to all or selected students via the bot.
- [ ] **Mini-Landing Page:** Enhancing the web interface into a proper booking page for students.

## Phase 3: Advanced Features
- [ ] **Smart Reminders:** Add an option for both tutor and student to enable/disable reminders (e.g., 24h and 2h before the lesson) via a button.
- [ ] **Google Calendar Integration:** Syncing bookings with the tutor's personal calendar (to be done last).

## Features NOT Planned (Removed)
- Financial accounting, payment tracking, and subscription management (keeping it simple for now).
