# Outbound agent alpha

The existing local publisher is unchanged. The new `python3 -m agent` entry point validates server connectivity only and **never claims jobs or operates a phone** until the deterministic adapter is implemented and accepted.

Create a private local JSON file (mode 0600) containing `server` (HTTPS origin), `token` (one-time device pairing credential). For local development only, `allow_loopback: true` permits HTTP on localhost. Do not store device PINs or API generation keys in this file. Run:

```sh
python3 -m agent --config /absolute/path/to/private-agent.json --once
python3 -m unittest discover -s tests -p 'test_agent_protocol.py' -v
```

SQLite journal entries use a unique server task ID and content digest. The local journal must transition to `submitting`, then receive the server boundary acknowledgement, before a native action. Any crash or uncertain acknowledgement requires reconciliation, never a new click. This release unit-tests the journal and HTTPS policy but does not yet integrate these hooks into existing phone controls.

Pending: single-device process lock, lease renewals, authenticated asset downloads and hash/order verification, deterministic native adapter, evidence upload, durable result retry, native draft/public checks, final independent screen-off reread. Until those pass, this agent must not replace the production publisher.
