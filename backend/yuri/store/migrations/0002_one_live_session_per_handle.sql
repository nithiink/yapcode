-- One live session row per native handle.
--
-- A full UNIQUE(native_session_id) would break adopt()'s legitimate
-- re-adoption of a stopped handle, so it was declined -- but the justification
-- ("only one row is ever live") was unenforced, and it is reachable: when
-- list_native() raises, _native() returns empty, adopt() takes the
-- not-yet-adopted branch and inserts a SECOND live row for the same handle.
-- Reproduced: two live rows for 'native-1' carrying two different missions,
-- which strands one of them.
--
-- So: a partial index, mirroring approvals_one_pending above it. The status
-- list is domain/session.py's LIVE_STATUSES; sqlite has no way to share that
-- constant, so store/sqlite.py asserts the two agree at migrate() time rather
-- than letting them drift silently.

-- Pre-existing duplicates would make CREATE UNIQUE INDEX fail and leave a
-- database that cannot be opened. Demote all but the most recently started of
-- each handle to 'lost', which is the honest status: a row whose handle
-- another live row already owns cannot be verified.
UPDATE sessions SET status = 'lost'
WHERE status IN ('starting', 'running', 'needs_permission', 'needs_choice', 'idle')
  AND id NOT IN (
    SELECT id FROM (
      SELECT id, ROW_NUMBER() OVER (
        PARTITION BY native_session_id ORDER BY started_at DESC, id DESC) AS rn
      FROM sessions
      WHERE status IN ('starting', 'running', 'needs_permission', 'needs_choice', 'idle')
    ) ranked WHERE rn = 1
  );

CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_live ON sessions(native_session_id)
  WHERE status IN ('starting', 'running', 'needs_permission', 'needs_choice', 'idle');
