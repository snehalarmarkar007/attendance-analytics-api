# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for the filtered, sorted list; `(department, joined_on)` for headcount and trend. `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race, monthly view); `(date desc, emp_code)` (default sort, month ranges); `(status, date desc, emp_code)` (status filter); `(emp_code, punch_in desc)` (finding the open record at punch-out). Rejected: an index on `late_minutes`/`work_hours`. The date range already narrows the data, and every extra index slows writes.

2. **Punch-in race.** Both requests call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which becomes 409. There is no "check first" step.

3. **Ties.** `$rank` gives 1, 2, 2, 4, then I keep `rank <= limit`. Everyone tied at the cutoff is returned, even if that exceeds `limit` rows. Order: minutes, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s their logs. `$unwind` with preserve-empty keeps employees with no logs.

5. **100x data.** Keep a monthly summary collection updated at punch/correction time, and use keyset pagination instead of `skip`.
