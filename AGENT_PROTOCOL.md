# Outbound agent alpha

The existing local publisher is unchanged. The new `python3 -m agent` entry point validates server connectivity and reads USB model/resolution/power status only. It **never claims jobs, wakes the phone, navigates UI or publishes** until the deterministic adapter is implemented and accepted. Every 30 seconds it reports observed USB state; disconnect or probe failure is not an online phone. Account verification timestamp is historical, never a claim of current account identity.

First verify the logged-in nickname on the phone and finish with verified screen-off. Use `python3 -m agent.pair --help` to bind that verified account: it accepts a private admin password file, probes exactly one USB phone and writes a private local config (0600). Do not pass PINs or model keys. Existing configs and existing account-device bindings are not overwritten. For local development only, `--allow-loopback` permits HTTP on localhost. Run:

```sh
python3 -m agent --config /absolute/path/to/private-agent.json --once
python3 -m unittest discover -s tests -p 'test_agent_protocol.py' -v
```

SQLite journal entries use a unique server task ID and content digest. The local journal must transition to `submitting`, then receive the server boundary acknowledgement, before a native action. Any crash or uncertain acknowledgement requires reconciliation, never a new click. This release unit-tests the journal and HTTPS policy but does not yet integrate these hooks into existing phone controls.

Pending: single-device process lock, lease renewals, authenticated asset downloads and hash/order verification, deterministic native adapter, evidence upload, durable result retry, native draft/public checks, final independent screen-off reread. Until those pass, this agent must not replace the production publisher.
