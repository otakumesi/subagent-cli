# Worker Continue Flow (v1)

1. Create handoff from previous worker:
`subagent worker handoff --worker w_123`

2. Start a new worker from the handoff artifact:
`subagent worker continue --from-worker w_123 --launcher codex --profile worker-default`

3. Watch normalized events from the new worker:
`subagent watch --worker w_456 --follow --ndjson`

4. Continue by sending a new instruction:
`subagent send --worker w_456 --text "Proceed with the minimal fix and add regression coverage."`
