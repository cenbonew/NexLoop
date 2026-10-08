# NX-016 Core0056 verification

The owned WebChat native ingress, governed catalog scope denial, actual JSON delivery and authenticated Human receipt are implemented. Actual DeepSeek evidence is a separate bounded synthetic validation, not external channel delivery or a production deployment.

- `real-model-core56-public-evidence.json`: the actual model attempt first failed because its test expected the old tool argument shape; strict current catalog scope equality fixed it. The second attempt passed in 16.31 seconds, with one actual tool request, actual HTTPS/fsynced JSON export, Human receipt and stable original event replay. Both private credential copies were removed; 49 owned files scanned, zero leaks.
- `browser56/`: real browser DOM evidence for scope denial, reload/requery and actual positive delivery. The provider was deterministic-test. Early 90-second previews and one invalid selector are preserved as prior failures. The original UI's missing execution label and readiness503 are explicit limitations.
- `head0056-ci-resolution-evidence.json`: first full CI exited1 with 1636 passed and one timestamp representation assertion failure. The full Relay module and new explicit CLI deployment label tests then passed25; Pi24 and Runtime124 tail gates were actually executed and passed with zero skips. This is failure resolution plus separate gates, not a claimed second monolithic CI pass.

## Manual actual model verification

Default CI never reads `.env` or invokes this manual runner. Explicitly invoke `tests/verification_real_model.py` with an owned private configuration file (0600). It requires the five exact fields `MODEL_PROVIDER`, `MODEL_ID`, `MODEL_BASE_URL`, `MODEL_CREDENTIALS_FILE`, `stage`; `stage` must be true. Use `deepseek`, `deepseek-flash`, `https://api.deepseek.com`, and an absolute path to an owned nonempty0600 credential file. No key is placed in a command or this document.

```sh
source ~/.nvm/nvm.sh
nvm use 24
NEXLOOP_REAL_MODEL_CONFIGURATION_FILE=<owned-private-configuration> \
PYTHONPATH=packages/eios-core/src:tests \
uv run --frozen pytest -q tests/verification_real_model.py --tb=no
```

The placeholder must be replaced by the private file path; it is not a literal runnable command. This runner uses only an owned disposable PostgreSQL/Artifact/SQLite/HTTPS delivery environment. It has a two-model-turn, two-tool-call, 120-second cap and USD0.62 reservation limit; this is not a claim about actual billing. Report output stays in the owned pytest temporary directory. It was collected once after moving the report output, without a new provider call; the actual passing call binds the candidate runner SHA in its evidence.

`--execution-profile deterministic-test|real-provider|disabled` is trusted deployment metadata only. It neither asserts a successful model call nor enables production readiness. `/health/ready` remains503 until operational readiness is implemented and verified. Third-party delivery, production deployment, generic exactly-once and reload recovery of a not-yet-ACKed browser event are outside this checkpoint.
