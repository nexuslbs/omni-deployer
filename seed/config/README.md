# Config directory for OMNI_DIR root-level yml files (actions/plugins/remote/settings/workflows).
# docker-compose.yml / docker-compose.dev.yml intentionally stay at the repo root.

## settings.yml — default channel settings

Four writable select settings control which channel producers use when an
explicit channel is not provided. The selects are enriched at runtime with
the channels defined in `channels.yml` (any platform — including
platform-less `cli` channels):

| Setting                     | Default  | Producer                                  |
|-----------------------------|----------|-------------------------------------------|
| `default_schedule_channel`  | `cron`   | Cron schedules with no explicit channel   |
| `default_hook_channel`      | (empty)  | Hooks with no explicit channel            |
| `default_kanban_channel`    | `kanban` | Kanban task dispatch with no task channel |

Resolution chain everywhere: explicit channel → the matching
`default_*_channel` setting → empty channel. When the chain comes up empty,
the thread is still INSERTED (with an empty `channel_id`) and then marked
failed with the error `no channel defined` — the record persists for audit
(fail-with-record).

## channels.yml — channels

The map KEY is the channel NAME — the stable identifier used everywhere
(API id, `threads.channel_id`, `messages.channel_id`,
`kanban_tasks.channel_id`, `summaries.channel_id`, tasks.yml `channel:`
references). A channel entry WITHOUT a `platform:` key is type `cli`: it
never attempts external delivery (the kanban/cron system channels are
platform-less `cli` channels).
