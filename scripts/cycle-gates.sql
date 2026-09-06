-- ============================================================================
-- cycle-gates.sql : one-shot "cycle gates" sheet for the omni profile
-- Internal-Improvement-Plan section 1 evidence table (E1-E7), workstream 3T,
-- candidate S12. (Internal-Improvement-Plan.md section 4 step 2 points here;
-- wiki mirror: Reference/Omniagent/Cycle-Gates-Metrics.md)
--
-- WHAT THIS IS
--   Every cycle re-measures the section 1 gate numbers for a time window. This
--   file is that measurement in ONE reusable SQL sheet: run the whole file via
--   psql, or run any single numbered statement (search_database accepts one
--   SELECT/WITH per call). Each statement is labelled with the gate (E1..E7).
--
-- WINDOW
--   All statements use the literal interval '7 days'. To re-measure a different
--   window, replace every occurrence of "interval '7 days'" with the window you
--   want (e.g. interval '14 days').
--
-- HOW TO RUN
--   psql:            psql "$DATABASE_URL" -f scripts/cycle-gates.sql
--   search_database: paste one statement at a time (read-only SELECT/WITH only).
--   NOTE (tool quirk, verified 2026-09-06): the search_database MCP returns NULL
--   for round()/sum()/char_length()/length()/octet_length()/position() and for
--   * and / arithmetic. All ACTIVE statements below avoid those: they use only
--   count()/avg()::bigint/max()/min() and LIKE filters, so the numbers come back
--   in BOTH vehicles. Statements needing the nulled functions are provided as
--   commented "psql only" variants for exact sums/shares/sizes.
--   ECHO CAVEAT: pasting a query into search_database records that query as a
--   message, so content-LIKE marker counts (E3, E6) then include your own echo.
--   Running the file via psql -f avoids that entirely; if pasting, subtract the
--   measuring thread (AND thread_id <> <your thread id>) for clean counts.
--
-- GATE MAP (see Internal-Improvement-Plan.md section 1):
--   E1 context re-read spend ........ G1
--   E2 unbounded context growth ..... G2
--   E3 re-read death spiral ......... G3
--   E4 retrieval under-use .......... G4
--   E6 compaction-summary spiral .... G6
--   E7 hallucination proxies ........ G7 (groundedness needs the search_metrics
--                                          tool, not raw SQL)
-- ============================================================================


-- ----------------------------------------------------------------------------
-- G1 . E1 : prompt vs completion per LLM call (window)
--   avg/max prompt_tokens per call; avg/max completion_tokens.
--   LLM call records = messages whose token_usage has BOTH prompt_tokens and
--   completion_tokens (assistant turns that consumed an LLM call).
--   Avg columns are cast to bigint (round() is NULL in search_database).
-- ----------------------------------------------------------------------------
SELECT
  count(*)                                                          AS llm_calls,
  avg((token_usage->>'prompt_tokens')::numeric)::bigint             AS avg_prompt_tokens,
  max((token_usage->>'prompt_tokens')::bigint)                      AS max_prompt_tokens,
  avg((token_usage->>'completion_tokens')::numeric)::bigint         AS avg_completion_tokens,
  max((token_usage->>'completion_tokens')::bigint)                  AS max_completion_tokens
FROM messages
WHERE created_at > now() - interval '7 days'
  AND token_usage ? 'prompt_tokens'
  AND token_usage ? 'completion_tokens';

-- E1-psql . completion share = sum(completion) / sum(prompt+completion)
-- (psql only: sum() and / return NULL in search_database)
-- SELECT
--   sum((token_usage->>'prompt_tokens')::bigint)     AS sum_prompt_tokens,
--   sum((token_usage->>'completion_tokens')::bigint) AS sum_completion_tokens,
--   round(100.0 * sum((token_usage->>'completion_tokens')::bigint)
--         / (sum((token_usage->>'prompt_tokens')::bigint)
--            + sum((token_usage->>'completion_tokens')::bigint)), 2) AS completion_share_pct
-- FROM messages
-- WHERE created_at > now() - interval '7 days'
--   AND token_usage ? 'prompt_tokens' AND token_usage ? 'completion_tokens';

-- E1c . biggest single calls this window (top 10; flags anomalies/test rows)
-- SELECT id, thread_id, msg_type, created_at,
--        (token_usage->>'prompt_tokens')::bigint     AS prompt_tokens,
--        (token_usage->>'completion_tokens')::bigint AS completion_tokens
-- FROM messages
-- WHERE created_at > now() - interval '7 days'
--   AND token_usage ? 'prompt_tokens' AND token_usage ? 'completion_tokens'
-- ORDER BY (token_usage->>'prompt_tokens')::bigint DESC
-- LIMIT 10;


-- ----------------------------------------------------------------------------
-- G2 . E2 : threads exceeding the hard budget (200K prompt tokens per call)
--   per-thread max prompt_tokens; count threads whose max call exceeds 200000.
--   Gate direction: no thread exceeds the hard budget in normal operation.
-- ----------------------------------------------------------------------------
WITH per_thread AS (
  SELECT thread_id,
         max((token_usage->>'prompt_tokens')::bigint) AS max_prompt,
         count(*)                                     AS llm_calls
  FROM messages
  WHERE created_at > now() - interval '7 days'
    AND token_usage ? 'prompt_tokens'
  GROUP BY thread_id
)
SELECT
  count(*)                                              AS threads_with_llm_calls,
  count(*) FILTER (WHERE max_prompt > 200000)           AS threads_over_200k_hard_budget,
  count(*) FILTER (WHERE max_prompt > 800000)           AS threads_over_800k,
  max(max_prompt)                                       AS global_max_prompt_tokens
FROM per_thread;

-- G2b . list of the offenders (top 25 by max prompt call)
-- SELECT thread_id, max_prompt, llm_calls
-- FROM per_thread ORDER BY max_prompt DESC LIMIT 25;


-- ----------------------------------------------------------------------------
-- G3 . E3 : re-read markers + auto-note markers (window)
--   Engine read-guard (omniagent src/agent/main_loop.rs:2054) emits
--     "[duplicate of {tool} at iteration {n} - ... rule 11]"
--   Notes READ-ONCE guard (plugins/tools/prompt/src/notes.rs:107 and the
--   plugins/tools/notes/src/notes.rs replica) emits
--     "[duplicate read of {name} (thread {id}) - forbidden by rule 12..."
--   Auto-notes writer (prompt/src/compact.rs:190) emits "## [engine:auto-note".
--   Gate direction: few duplicates; the agent uses notes instead of re-reading.
--   ECHO CAVEAT: see file header. When pasting into search_database, add
--   "AND thread_id <> <measuring thread id>" for clean counts.
-- ----------------------------------------------------------------------------
SELECT
  count(*) FILTER (WHERE content LIKE '%[duplicate of %')            AS engine_duplicate_markers,
  count(*) FILTER (WHERE content LIKE '%[duplicate read of %')       AS notes_duplicate_read_markers,
  count(*) FILTER (WHERE content LIKE '%[engine:auto-note%')         AS auto_note_markers
FROM messages
WHERE created_at > now() - interval '7 days';

-- G3b . which threads carry the duplicate markers (top 15)
-- SELECT thread_id, count(*) AS n
-- FROM messages
-- WHERE created_at > now() - interval '7 days'
--   AND (content LIKE '%[duplicate of %' OR content LIKE '%[duplicate read of %')
-- GROUP BY thread_id ORDER BY n DESC LIMIT 15;


-- ----------------------------------------------------------------------------
-- G4 . E4 : retrieval-tool usage (window)
--   retrieval_tool_msgs = tool/multi-tool messages that invoked search_messages
--     or search_wiki (content lines "search_messages: {..}" / "search_wiki:")
--   llm_responses      = LLM call records (rows with token_usage ? prompt_tokens)
--   share = retrieval_tool_msgs / llm_responses (compute by hand or psql; the
--   section 1 baseline was 28 retrieval calls in 693 responses, 4%).
-- ----------------------------------------------------------------------------
WITH win AS (
  SELECT * FROM messages WHERE created_at > now() - interval '7 days'
)
SELECT
  (SELECT count(*) FROM win WHERE msg_type IN ('tool','multi-tool')
     AND (content LIKE '%search_messages:%' OR content LIKE '%search_wiki:%'))
                                                          AS retrieval_tool_msgs,
  (SELECT count(*) FROM win WHERE token_usage ? 'prompt_tokens')
                                                          AS llm_responses;

-- G4b . per-tool split (message counts mentioning each retrieval tool)
-- SELECT
--   count(*) FILTER (WHERE content LIKE '%search_messages:%') AS search_messages_msgs,
--   count(*) FILTER (WHERE content LIKE '%search_wiki:%')     AS search_wiki_msgs
-- FROM messages
-- WHERE created_at > now() - interval '7 days'
--   AND msg_type IN ('tool','multi-tool')
--   AND (content LIKE '%search_messages:%' OR content LIKE '%search_wiki:%');


-- ----------------------------------------------------------------------------
-- G6 . E6 : compaction events per thread (window)
--   compaction marker text: "=== Compaction Summary ===" (prompt compact.rs:13).
--   count(*) = compaction events observed per thread. The frozen block is capped
--   at 50000 chars by the prompt plugin (max_summary_chars).
--   NOTE: content length (size vs cap) is psql only (search_database NULLs
--   length()) - see G6-psql.
-- ----------------------------------------------------------------------------
SELECT thread_id,
       count(*) AS compaction_events
FROM messages
WHERE created_at > now() - interval '7 days'
  AND content LIKE '%=== Compaction Summary ===%'
GROUP BY thread_id
ORDER BY compaction_events DESC;

-- G6-psql . per-thread event count + max content length of marker rows
-- SELECT thread_id,
--        count(*)              AS compaction_events,
--        max(length(content))  AS max_marker_msg_chars
-- FROM messages
-- WHERE created_at > now() - interval '7 days'
--   AND content LIKE '%=== Compaction Summary ===%'
-- GROUP BY thread_id ORDER BY compaction_events DESC;


-- ----------------------------------------------------------------------------
-- G7 . E7 : rework/retest proxies from kanban history (window)
--   rework = workflow event review -> running (reviewer sent the task back to
--            the executor)
--   retest = review -> testing (reviewer sent the task back to the tester)
--   approved = review -> done
--   testing_rerun = testing -> running
--   Gate direction: groundedness >= 90%; corrections/rework do not rise while
--   iterations fall. GROUNDEDNESS itself is reported by the search_metrics tool
--   (run: search_metrics hours=<window hours>) and is not a raw SQL column.
-- ----------------------------------------------------------------------------
SELECT
  count(*) FILTER (WHERE action='workflow' AND initial_board='review'
                     AND final_board='running')   AS rework,
  count(*) FILTER (WHERE action='workflow' AND initial_board='review'
                     AND final_board='testing')   AS retest,
  count(*) FILTER (WHERE action='workflow' AND initial_board='review'
                     AND final_board='done')      AS approved,
  count(*) FILTER (WHERE action='workflow' AND initial_board='testing'
                     AND final_board='running')   AS testing_rerun
FROM kanban_history
WHERE created_at > now() - interval '7 days';

-- E7b . approval-vs-rework ratio (psql only)
-- SELECT round(100.0 * (rework + retest) / nullif(rework + retest + approved, 0), 2)
-- FROM (...same subquery as G7... ) t;
