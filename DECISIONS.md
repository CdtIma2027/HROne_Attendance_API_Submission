# Design decisions

I create a unique employee-code index so concurrent duplicate employee creation cannot succeed twice, and a unique `(emp_code, date)` index for the same guarantee on punch-in. The attendance date/employee index supports the ordered attendance list. Employee department and join-date indexes support headcount queries; the punch-in index finds the latest open record; the date/late index supports monthly lateness scans. I did not add an email uniqueness rule because the contract only requires employee-code uniqueness.

Two punch-ins can both read the employee and build a record, but MongoDB's unique attendance index allows only one insert for that employee/date. The winner returns 201; the duplicate-key loser maps to 409. On the late leaderboard, equal totals share `$rank`; the next rank is skipped. I filter after ranking, so all employees tied at the limit rank are returned, sorted by employee code within the tie.

The department summary starts from employees joined by the month end, then performs a lookup that preserves employees with no attendance documents. That keeps headcount independent of activity. For 100 times the data, I would benchmark explain plans and likely move high-volume date scans to monthly rollups, while preserving the source logs for audit.
