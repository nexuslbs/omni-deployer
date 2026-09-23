# Canonical A/B corpus run: gate comparison

- run id: `corpus-20260923-183411`
- timestamp: 20260923-183411
- side a label: `main` (binary sha a70cace0af0bc43e38af6204ab4362207a68dc98)
- side b label: `main-identity` (binary sha a70cace0af0bc43e38af6204ab4362207a68dc98)
- candidate ref: (identity)
- outdir: `/opt/workspace/tmp/corpus-ab/20260923-183411`
- toolset ensure: {"filesystem_info": true, "search_messages": true, "notes_note-list": true, "git_status": true, "memory_list-memories": false, "subtasks_get-subtask-counts": true}

## Helpfulness (T-1): side a

| task | shape | status | outcome | iters | dur_s | dup | comp | retr | grounded | in_tok | out_tok |
|---|---|---|---|---|---|---|---|---|---|---|---|
| t02-verification-corpus-files | re-read-heavy-verification | completed | PASS | 7 | 27 | 1 | 0 | 0 | 1/1 | 147114 | 4018 |

## Helpfulness (T-1): side b

| task | shape | status | outcome | iters | dur_s | dup | comp | retr | grounded | in_tok | out_tok |
|---|---|---|---|---|---|---|---|---|---|---|---|
| t02-verification-corpus-files | re-read-heavy-verification | completed | PASS | 7 | 22 | 1 | 0 | 0 | 1/1 | 135843 | 2846 |

## Comparison (A vs B)

| task | A status | B status | A iters | B iters | A outcome | B outcome | A dup | B dup | A comp | B comp |
|---|---|---|---|---|---|---|---|---|---|---|
| t02-verification-corpus-files | completed | completed | 7 | 7 | PASS | PASS | 1 | 1 | 0 | 0 |

| gate | side a | side b |
|---|---|---|
| tasks completed | 1/1 | 1/1 |
| tasks pass | 1/1 | 1/1 |
| iterations median | 7 | 7 |
| iterations p90 | 7 | 7 |
| duplicate markers total | 1 | 1 |

## Speed (T-2) probes

| probe | side a | side b |
|---|---|---|
| search_messages_p50_ms | None | None |
| search_messages_p95_ms | None | None |
| search_wiki_p50_ms | None | None |
| search_wiki_p95_ms | None | None |
| prompt_build_ms_avg | 7.7 | 8.4 |

## Resources (T-3)

| resource | A before | A after | B before | B after |
|---|---|---|---|---|
| db_size_bytes | 46.5 MiB | 47.0 MiB | 47.6 MiB | 48.2 MiB |
| rel_messages_bytes | 1.0 MiB | 1.1 MiB | 1.1 MiB | 1.2 MiB |
| rel_threads_bytes | 48.0 KiB | 48.0 KiB | 48.0 KiB | 48.0 KiB |
| rel_summaries_bytes | 0 B | 0 B | 0 B | 0 B |
| data_dir_bytes | n/a | n/a | n/a | n/a |
| spill_bytes | n/a | n/a | n/a | n/a |
| rss omniagent | 34.09MiB / 7.752GiB | 42.05MiB / 7.752GiB |
| rss postgres | 119.4MiB / 7.752GiB | 166.5MiB / 7.752GiB |


