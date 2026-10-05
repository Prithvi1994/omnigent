# Tunnel lifecycle diagnostics

Structured fields that explain a runner tunnel drop end to end: what closed
the socket, how long the runner was gone, whether the server failed the turn,
and whether the reconnect restarted a turn. They ride the existing debug-log
sink as `event_name` plus string `attributes`; booleans appear as `True` or
`False`. Deploy both the server and the runner before expecting the fields on
both ends.

## Runner events (`source = 'runner'`)

- `runner_connected`: `connection_id`, `reconnect` (an earlier connection on
  this process was accepted), `attempt` (ordinal within the reconnect
  streak), `downtime_s` (gap since the previous connection ended), `pid`.
- `runner_tunnel_disconnected`: one row per attempt the runner retries,
  replacing the plain retry line. A fatal exit (persistent auth or protocol
  rejection, cancellation) raises out of the reconnect loop and is logged by
  the caller instead. `disconnect_reason` (the bounded classifier shared with
  the OTel counter; `local_shutdown` when the process is stopping, even if the
  close handshake broke), `close_code`, `close_reason`, `close_rcvd_code` and
  `close_sent_code` (which side sent a close frame; a 1006 has neither),
  `error_type`, `connected`, `connection_age_s`, `recycle`, `backoff_reset`,
  `delay_s`, `retry_in_s`. A clean 1000/1001 close ends the read loop without
  an exception, so its codes and reason come from the connection's own close
  frames.
- `runner_session_initialized`: `recovery_turn` (`history_resume`,
  `recovery_prompt` or `none`) with its inputs `recovery_id`,
  `resume_interrupted_turn`, `suppress_recovery_turn`, `execution_seen`,
  `history_len`, `last_item_type`, and the resulting `status`.

## Server events (`source = 'server'`)

- `runner_tunnel` with `phase` `connected`, `disconnected` or
  `error`: `connection_id` from the runner's hello, `connection_age_s`,
  `last_frame_age_s`, `ended_by` (the helper tasks that had finished when the
  end was observed, comma-separated: `tunnel-receive`, `tunnel-ping`, or
  `tunnel-sender`), plus the close `code` and `reason` on `disconnected`.
  When a helper reports a peer disconnect, the event preserves its observed
  code and reason. Otherwise it records the first server-requested close,
  including retirement, replacement, or ping timeout, without implying that
  the peer acknowledged it. Concurrent close requests can make the recorded
  code and reason differ from those sent on the socket. A stale receive or
  ping helper can end before the sender, so `ended_by` alone does not identify
  the close cause.

  During a rollout, queries should accept both `closed` (older servers) and
  `disconnected`, and allow missing close details on older `closed` rows.
  Update queries that select only `closed` to use `disconnected` after the
  server upgrade is complete.
- `runner_ping_timeout`: `runner_id`, `connection_id`, `connection_age_s`,
  `silent_s`.
- `runner_stream_transport_lost`: one row per outage when the relay first
  observes the loss, with `intentional_stop` and `grace_s`. An unintentional
  loss is then held for `grace_s`; an intentional stop goes straight to the
  give-up row.
- `runner_stream_disconnected`: the relay's give-up row, with `decision`
  (`intentional_stop`, `server_shutdown`, `idle_no_failure` or
  `failed_mid_turn`), `grace_s`, `outage_s`, `retries`. `outage_s` is the
  time since the current grace window opened; a reconnect that dropped again
  within the window does not reset it, so it includes that brief connected
  stretch and is not cumulative disconnected time.
- `runner_session_init_started`: `resume_interrupted_turn`,
  `suppress_recovery_turn`, `recovery_id`. Neither flag set is the tunnel
  reconnect hook; resume set is a sub-agent restore; suppress set is a
  message forward.

## Correlation

Join the runner's and server's rows for one socket on
`attributes['connection_id']`. A `runner_connected` row with `reconnect =
False` after earlier rows for the same `runner_id` is a new process; `pid`
confirms it. A repeating `connection_age_s` across drops points at an
intermediary timeout rather than either endpoint.

## Heartbeat and send diagnostics

The tunnel connection, disconnect, and `runner_ping_timeout` rows also carry
local timing observations. `runner_tunnel_health` reports a scheduling delay,
send, or queue wait of at least one second, at most once per minute per
connection. A sampler runs every five seconds; a pending send is visible even
if it never completes. Ordinary heartbeats add no log rows.

Join on `connection_id` and compare `tunnel_side = server` with `runner`:

| Fields | Meaning |
| --- | --- |
| `loop_lag_s`, `loop_lag_max_s` | Delay past the sampler's scheduled wakeup. A local scheduling gap, including process suspension on platforms whose monotonic clock advances during suspension; it does not identify the blocking code. |
| `sends_in_flight`, `oldest_tracked_send_age_s` | Sends awaiting the local WebSocket API at the observation time. A blocked send can coexist with a responsive loop. |
| `send_duration_s`, `send_duration_max_s`, `last_send_outcome` | Completed, failed, or cancelled send duration. Includes loop scheduling delays; send completion proves local acceptance, not peer receipt. |
| `outbound_queue_depth`, `outbound_queue_high_water` | Server frames waiting behind its sole sender. Absent on the runner, which sends directly. |
| `enqueue_delay_s`, `queue_wait_s` (and `_max_s`) | Server time from requesting an enqueue to execution on the socket loop, and from enqueue to dequeue, respectively. Queue timing metadata never travels over the wire. |
| `app_pings_queued`, `last_app_ping_queued_age_s`, `last_app_ping_sent_age_s`, `last_app_pong_received_age_s` | Server application-heartbeat progress through enqueue, successful send, and pong receipt. |
| `last_app_ping_received_age_s`, `last_app_pong_sent_age_s` | Runner application-heartbeat receive and successful response-send times. |
| `app_ping_rtt_s` | Server-local elapsed time from starting the matching ping send to consuming its pong; excludes queue wait, may include send delay. Uses the echoed token only for matching, not as a clock. |
| `last_received_frame_age_s`, `last_sent_frame_age_s` | Time since any received application frame or successfully completed application-frame send. |

All durations use local monotonic time. Maxima cover this connection's lifetime;
their accompanying `_max_age_s` fields distinguish old congestion from delays
near the failure. Disconnect observations are frozen before helper cancellation.
`diagnostics_age_s` is time since that snapshot, so add it to an age when
comparing to the log row's timestamp. Missing timing fields mean no observation,
not zero elapsed time. The debug sink omits nulls.

History is bounded to eight outstanding application ping tokens and 64 active
send samples. `app_ping_samples_dropped` and `send_samples_dropped` expose any
sampling limit; `sends_in_flight` still counts all sends. Frames already waiting
in the server queue retain only their own timestamp and optional ping token.

Application heartbeats and WebSocket protocol keepalives are separate. These
fields do not observe protocol control-frame ping/pong traffic. Runner
`protocol_ping_interval_s` and `protocol_ping_timeout_s` come from the live
WebSocket connection (`protocol_keepalive_source = websockets_connection`).
With that source, an absent interval or timeout means the corresponding library
setting is disabled. Server rows expose the actual `app_ping_interval_s` and
`app_silence_timeout_s`; `protocol_keepalive_source = unavailable_from_asgi`
explicitly leaves the server's protocol settings unknown. Shared constants alone
do not prove a deployment's Uvicorn configuration.

A large loop delay localizes a scheduling interruption to that process, without
proving CPU starvation versus suspension. Long sends or queue waits with small
loop delays suggest backpressure. Missing heartbeats with neither observation
still leave the peer or network path unresolved; compare both sides before
assigning a cause. These observations do not change liveness, retry, or turn
failure decisions.

## Build identity

Databricks App deploys append the checked-out commit to the stamped version
(`0.16.0.post1790000000+g1a2b3c4`, with `.dirty` when the tree has
uncommitted or untracked non-ignored files, which only `--allow-dirty`
permits). The stamp is written to the pyprojects and to
`omnigent/version.py`, the constant the runtime imports, so `app_version` on
every row and `version` on the server's `runner_tunnel` connected row name
the build. The generated version is itself valid for
`--skip-build --version <version>`.

## Verification

```sh
uv run --no-sync pytest -q tests/runner/transports/ws_tunnel/test_serve.py \
  tests/runner/transports/ws_tunnel/test_diagnostics.py \
  tests/runner/transports/ws_tunnel/test_frames.py \
  tests/server/integration/test_runner_tunnel_route.py \
  tests/server/routes/test_sessions_runner_relay.py \
  tests/server/test_runner_session_init.py \
  tests/runner/test_suppress_recovery_turn.py \
  tests/deploy/test_databricks_deploy_version.py
```

Against a live server and runner with the debug sink configured: drop the
runner's socket, hold a reconnect past `RUNNER_DISCONNECT_GRACE_S`, kill the
runner process, and crash a harness mid-turn. One query on the session over
the events above, ordered by `client_time`, must tell the four apart and show
whether the original turn survived.

For a credential-free recovery check, run
`uv run --no-sync pytest -q tests/e2e/test_runner_tunnel_mid_turn_reconnect_grace_e2e.py`.
This uses real server and runner processes with a mock LLM, including a
45-second tunnel blackout and reconnects to another replica. The original
turn must complete without a failed status edge.

On a disposable local runner, pause only that runner process with
`kill -STOP "$runner_pid"`, wait eight seconds, then `kill -CONT "$runner_pid"`.
Check its `runner_tunnel_health` row for a loop delay and match its
`connection_id` to the server's rows. A send delay alone should not be labelled
an event-loop stall. Use the blocked-send and stalled-loop tests above for
deterministic examples of both signatures.
