# Contributing

Thanks for taking the time to improve meta-ads-guard. Bug reports, new metric
aliases, notifier integrations and documentation fixes are all welcome.

## Reporting a bug

Open an issue with:

- the command you ran and the full output or traceback (run with `-vv`)
- your Python version (`python --version`), operating system and the
  `api.version` from your config
- the rule that misbehaved and, if relevant, an insights row that reproduces
  it, **anonymised**: replace account, campaign, ad set and ad ids with made-up
  numbers, and remove names, tokens and webhook URLs

Never paste an access token, app secret, bot token or webhook URL into an
issue. If you did by accident, revoke it immediately.

## Development setup

You need Python 3.9 or newer.

```console
$ git clone https://github.com/saizul/meta-ads-guard.git
$ cd meta-ads-guard
$ python -m venv .venv
$ . .venv/bin/activate
$ python -m pip install -e ".[dev]"
```

Then, before every commit:

```console
$ ruff check .
$ pytest
```

Both must pass; CI runs them on Python 3.9 to 3.13, plus Windows and macOS.

## Guidelines

- **Tests never touch the network.** The Graph client, the notifiers and the
  async report poller all take an injectable transport, sleep function or
  SMTP factory. Use `tests/conftest.py::FakeTransport` and
  `tests/fakeaccount.py`.
- **Safe by default.** Anything that changes an ad account must stay behind
  `--apply`, respect cooldowns and the `safety` limits, and be written to the
  audit log.
- **Verify Marketing API details** (field names, edges, error codes) against
  the official documentation at developers.facebook.com and mention the page
  in the pull request.
- **Every change comes with a test** that fails without it.
- Keep lines within 100 characters and let `ruff check .` decide style
  questions. Keep the code valid on Python 3.9 (no `match`, no `X | Y` at
  runtime).
- Add a line to the `Unreleased` section of `CHANGELOG.md` for anything a user
  would notice.

## Project layout

```text
src/meta_ads_guard/
  cli.py          command-line interface
  config.py       YAML loading, validation, notifier construction
  rules.py        rules, actions and filters
  expr.py         condition parser and evaluator
  metrics.py      insights rows -> metric values
  engine.py       evaluation, cooldowns, actions, notifications
  marketing.py    ad objects, insights (sync and async), mutations
  graph.py        Graph API client, retries, pagination
  ratelimit.py    usage headers and back-off
  http.py         transport abstraction (urllib)
  state.py        JSON / SQLite cooldown store
  audit.py        JSON Lines audit log
  timeutil.py     windows and durations
  currency.py     budget minor units
  notify/         Telegram, webhook, email
tests/            pytest suite with a mocked HTTP layer
examples/         configs, scripts and scheduling examples
```
