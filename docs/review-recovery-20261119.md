# Screener review recovery

The browser previously stopped following a statistics job after six five-second polls, even though historical quotes took minutes. Deferred responses could be cached as finished, returning to the tab did not reliably resume reads, and the refresh button bypassed the review request. Statistics existed only in process memory and disappeared at deployment; the background producer computed Blackhorse and Radar but omitted the Turning card.

Correct these paths: bounded fifteen-second reads follow jobs for up to thirty minutes, stop after completion/hidden/departed pages, retry transient failures while retaining previous results, and accept explicit refresh. Deferred states are not browser-cached. Actual saved computation time is shown, rather than the time of every read.

The producer fetches each mature recommended symbol once across Blackhorse, Radar/legacy Radar Live and Turning. Unmatured recommendations do not cause historical downloads. Four-symbol single-worker batches retain the existing 160 MiB admission checks; historical requests skip the unnecessary MIS path and use five-second request timeouts. Existing split/adjustment, return, horizon and benchmark formulas remain. All-source quote failure and persistence failure preserve previous statistics.

Completed JSON-safe statistics use the existing shared_data_snapshots table under workbench_review:v1. No schema or account/transaction changes. On restart, serve the last successful snapshot immediately; stale statistics retain their actual timestamp while refreshing. Use a fifteen-minute update interval instead of rebuilding after every five minutes.

The scoped-OIDC review-refresh workflow runs at Taiwan 14:22/16:22/20:22 on weekdays. GitHub schedules are best effort. GET is status-only; POST requests due work. The verification runner waits for an actually saved snapshot, not an enqueue acknowledgment. It sends no LINE messages and creates no new paid service.

Validation covers shared mature quotes, all three sources, restart recovery, stale-result retention, persistence failure, endpoint authorization/read-only GET, actual refresh behavior, polling past the old cutoff, completion, transient retry and SPA cleanup. After deploy, dispatch the workflow and inspect saved timestamp and mature sample counts for each mode. Rollback by reverting code/workflow; the task-specific snapshot key may remain unused, so no destructive rollback or migration is needed.
