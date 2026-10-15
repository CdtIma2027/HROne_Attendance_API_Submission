# Starter review

| # | Where | What is wrong | How you'd notice | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares naive UTC against a wall-clock shift time, so the offset is wrong. | Punch at 09:45 IST reports an incorrect late value. | Convert to IST and compare against the shift start on the attendance day. |
| 2 | `compute_late_minutes` | Uses truncation after subtracting times without applying the strict 10-minute rule correctly at the boundary. | 09:40:01 must count as 10 late minutes. | Apply the strict seconds comparison, then floor elapsed whole minutes. |
| 3 | `compute_work_hours` | Python `round` uses ties-to-even and floats. | Duration exactly halfway at 2 decimals can round the wrong way. | Decimal half-up rounding. |
| 4 | `compute_overtime` | Builds the shift end on the same date, failing overnight shifts. | A 22:00-06:00 shift has erroneous overtime. | Compute the end on the next attendance day for overnight shifts. |
| 5 | `/health` | Always reports ready without checking MongoDB. | Stop MongoDB; health still returns 200. | Ping MongoDB and return 503 on failure. |
| 6 | `create_employee` | Duplicate check races and `created_at` is naive. | Parallel creates can both pass the check; timestamp is interpreted ambiguously. | Enforce a unique index, catch duplicate key, store UTC-aware time. |
| 7 | `list_employees` | Skip formula is off by one page; total ignores department; no sort or bounds. | Page 1 skips records and filtered total is wrong. | Use `(page-1)*page_size`, filtered count, sort, validate limits. |
| 8 | `punch_in` | Doesn't handle unknown employees, uses server local time/UTC date, accepts bad statuses, and doesn't truncate seconds. | Unknown code causes 500; IST midnight or overnight shift is assigned incorrectly. | Validate employee/status, truncate timestamps and derive R1 attendance day. |
| 9 | `punch_in` | Check-then-insert is not concurrency-safe; generated Mongo id is leaked as `id`. | Parallel requests create duplicates or response contains an undocumented id. | Unique `(emp_code,date)` index and duplicate-key 409; omit `_id` and `id`. |
| 10 | `/attendance` | Loads every match into Python, sorts after fetching, returns an unfiltered total, and uses unbounded page inputs. | Large collection is slow or memory-heavy; filters give misleading totals. | Mongo sort/skip/limit and filtered count with validated pagination. |
| 11 | `/attendance` | Doesn't validate date strings/range and sorts only by date. | Reversed or malformed dates behave unpredictably; same-day order is unstable. | Validate inclusive dates and sort by date descending, employee ascending. |
| 12 | API serialization | BSON datetime values are returned directly and nested history timestamps aren't handled. | JSON responses fail or expose non-contract types. | Convert all instants, including history change values, to epoch milliseconds. |

## Checked and kept

The starter's `compute_work_hours` formula is dimensionally correct before rounding, and the attendance record's default null/zero fields match the data model. The starter's health response shape is also correct; its database-readiness behavior was not.
