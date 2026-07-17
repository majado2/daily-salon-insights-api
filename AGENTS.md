# Backend Instructions

- Treat `../docs/prdv1.md`, `../docs/database-schema.md`, and `../docs/api-contract.md` as the approved business contract.
- Keep business rules in services, not route handlers.
- Use `Asia/Riyadh` for work dates and UTC for stored timestamps.
- Never accept derived settlement fields from clients.
- Never expose PIN hashes, session tokens, phone numbers in summaries, or administrative correction details to cashier responses.
- Add or update tests whenever a core business rule changes.
- Keep MySQL production compatibility; SQLite is only a fast local and unit-test fallback.

