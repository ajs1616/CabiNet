# 🔧 Tools

The self-contained regression gates (test_*.py — each must end "N passed, 0 failed"), the bench preflight, the tty cockpit (debug_console.py), and the dev-rig replay gate (avp_replay.py; needs wire captures that don't ship here).
## glass_button_trace.py — the SERVICE-button latency tape

Read-only. Run it on the hub after a night of pressing the cabinet's SERVICE
button and it lines up, per press, what the hub already wrote to
`G2S/logs/`: the CBE301 event, the hook's decision (show / hide / recovery
push / debounced), the moment the show or hide POST left the FIFO, and the
moment the cabinet answered — with the ack verdict. Ends with the branch
counts, median/p90/max, and the patterns worth a look (a show followed by
a hide seconds later from one press, a press that hit the 60 s recovery
throttle, a decision that never became a POST).

    python3 G2S/tools/glass_button_trace.py --since 3h
