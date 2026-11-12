# meta-ads-guard

[![CI](https://github.com/saizul/meta-ads-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/meta-ads-guard/actions/workflows/ci.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

A rules engine for Meta (Facebook and Instagram) ad accounts. You write rules
in YAML, and meta-ads-guard checks them against Marketing API insights. It can
pause, scale budgets, label or alert:

```yaml
- name: stop-spend-without-purchases
  level: adset
  when: spend > 1500 BDT and purchases == 0 in last 3 days
  actions: [pause, notify]
  cooldown: 12h
```

Meta's built-in automated rules can't express everything. They can't compare
today with the last 7 days, cap a budget at a fixed amount, or tell you in a
Telegram group why something was paused. They also leave no audit trail you
own. This tool runs from cron, a container or a scheduled workflow. It is
**dry-run by default**, so nothing changes until you pass `--apply`.

## Features

- **Rules in YAML**: conditions over metrics and lookback windows. The
  expression language supports `and`/`or`/`not`, arithmetic, `×`, chained
  comparisons (`0.5 < roas < 1.2`), currency suffixes (`1500 BDT`), variables
  (`cpa > target_cpa × 1.4`) and functions (`min`, `max`, `abs`, `coalesce`,
  `round`, `missing`). Conditions are parsed, never passed to `eval`.
- **Lookback windows** per rule (`window: 7d`), per metric (`spend[today]`,
  `frequency[7d]`) or as a trailing clause (`... in last 3 days`). Windows are
  computed in the ad account's time zone.
- **Metrics**: spend, impressions, reach, clicks, link clicks, CTR, link CTR,
  CPC, CPM, frequency, results and cost per result for a result type you
  choose (purchases, leads, Messenger conversations, add to cart, checkout,
  registrations, or any action type), conversion value, ROAS, raw
  `actions.<type>` values and object fields such as `daily_budget` and
  `age_days`.
- **Actions**: `pause`, `notify`, `scale_budget` by a percentage within
  `min`/`max` caps (in major currency units), and `label`.
- **Safety**: dry run by default, cooldowns per object, a cap on actions per
  run, a cap on budget change per action, and validation that catches unknown
  metrics, mismatched currencies and risky settings before anything runs.
- **State and audit**: cooldowns are stored in a JSON or SQLite file, and every
  planned or applied action goes to a JSON Lines audit log.
- **Built for the Marketing API**: a pinned API version, cursor pagination,
  async report jobs for long ranges, and `appsecret_proof`. Back-off is driven
  by `X-Business-Use-Case-Usage`, `X-Ad-Account-Usage`,
  `X-FB-Ads-Insights-Throttle` and `X-App-Usage`. Throttling errors 4, 17, 32,
  613 and 80000-80014 are retried, except quota errors such as 613/1487632,
  where an immediate retry can never succeed.
- **Notifiers**: Telegram, Slack-compatible webhooks (Slack, Mattermost,
  Google Chat, Discord, n8n and others) and SMTP email. You can get one digest
  per run or one message per match.
- **Small footprint**: PyYAML and tzdata are the only dependencies, and HTTP
  uses the standard library.

## Installation

The project is not on PyPI yet. Install it from a checkout:

```console
$ git clone https://github.com/saizul/meta-ads-guard.git
$ cd meta-ads-guard
$ python -m venv .venv && . .venv/bin/activate
$ pip install .
$ meta-ads-guard --version
```

Or straight from GitHub:

```console
$ pip install "git+https://github.com/saizul/meta-ads-guard.git"
```

### Access token

You need a Marketing API access token with `ads_read`, plus `ads_management`
if rules should change anything. A **system user** token from Business
Manager is the right choice for a scheduled tool, because it does not expire
like a user token. Export it as an environment variable:

```console
$ export META_ACCESS_TOKEN="your-system-user-token"
$ export META_APP_SECRET="your-app-secret"     # optional, enables appsecret_proof
```

## Quick start

```console
$ cp examples/guard.example.yaml guard.yaml       # edit account id, rules, notifiers
$ meta-ads-guard validate -c guard.yaml           # offline check, no token needed
$ meta-ads-guard insights -c guard.yaml --level adset --window 7d
$ meta-ads-guard run -c guard.yaml                # dry run: shows the plan
$ meta-ads-guard run -c guard.yaml --apply        # act and notify
```

A dry run prints what would happen:

```text
meta-ads-guard run bcb8edf8bb2e: DRY RUN (nothing changed; add --apply to act)
account Demo Shop (act_1234567890), currency BDT, time zone Asia/Dhaka

rule                          level  name               status   action                                                                  values
----------------------------  -----  -----------------  -------  ----------------------------------------------------------------------  ----------------------
stop-spend-without-purchases  adset  BD | Cold | Video  planned  pause: ACTIVE -> PAUSED                                                 spend=1,650.50, purchases=0
cpa-above-target              adset  BD | Retargeting   planned  scale budget -20% (min 500): daily_budget 2,000.00 BDT -> 1,600.00 BDT  cpa=1,100, target_cpa=450, results=2
scale-winners                 adset  BD | Lookalike 1%  planned  scale budget +15% (max 20000): daily_budget 3,000.00 BDT -> 3,450.00 BDT  roas=5, spend=5,000
```

To try this without an account, run `python examples/offline_demo.py`. It runs
the real engine against a fake in-memory account.

## Usage

```text
meta-ads-guard run        evaluate rules; plan (default) or --apply
                          --rule NAME (repeatable)  --notify (send during a dry run)  --json
meta-ads-guard validate   check the config file without calling the API
meta-ads-guard insights   metrics per object: --level --window --result --sort --top --json
meta-ads-guard cooldowns  list cooldowns, or --clear [--rule NAME]
meta-ads-guard audit      latest audit log entries: --tail N --json
meta-ads-guard metrics    list every metric name a rule can use
```

The config path defaults to `guard.yaml`, or to `$META_ADS_GUARD_CONFIG` when
that is set. Use `-v` or `-vv` for more logging.

Exit codes are `0` for success, `1` when the run finished with errors (a
failed action or notifier, or an API error), `2` for a configuration error and
`3` for an authentication or permission error.

## Writing rules

```yaml
rules:
  - name: cpa-above-target          # unique; letters, digits, . _ -
    description: optional text
    level: adset                    # campaign | adset | ad
    window: 7d                      # default window for metrics without [..]
    result: purchases               # what results / cpa / roas count
    vars: { target_cpa: 500 }       # merged over the global vars
    when: cpa > target_cpa × 1.4 and results >= 3
    filters:
      effective_status: [ACTIVE]    # default
      name_contains: [BD]
      labels_none: [no-autoscale]
    actions:
      - scale_budget: { percent: -20, min: 500 }
      - notify
    cooldown: 24h
    enabled: true
```

`when` can also be a list. Its items are joined with `and`.

### Windows

| Spelling                    | Meaning                                                |
|-----------------------------|--------------------------------------------------------|
| `today`                     | the current day, in the account time zone              |
| `yesterday`                 | the previous full day                                  |
| `3d`, `3 days`, `last 3 days` | rolling: today plus the 2 days before (spend so far today counts) |
| `last_7d`                   | Meta preset style: the 7 full days before today        |
| `this_month`, `last_month`  | calendar months                                        |
| `lifetime`                  | everything (`date_preset=maximum`)                     |

A metric can take its own window, for example
`spend[today] > 2 * spend[last_7d] / 7`. Each level and window pair is fetched
once per run, however many rules use it.

### Metrics

Run `meta-ads-guard metrics` for the full list. The main ones:

| Metric | Meaning |
|---|---|
| `spend`, `impressions`, `reach`, `clicks`, `link_clicks` | counters |
| `ctr`, `link_ctr` | percent (`1.2` means 1.2 %) |
| `cpc`, `cpm` | cost per click, cost per 1,000 impressions |
| `frequency` | impressions / reach |
| `results`, `cost_per_result` (alias `cpa`), `result_value`, `roas` | for the rule's `result` type |
| `purchases`, `leads`, `messages`, `add_to_cart`, `initiate_checkout`, `complete_registration`, `landing_page_views` | conversions |
| `purchase_value`, `purchase_roas`, `cost_per_purchase`, `cost_per_lead`, `cost_per_message` | |
| `actions.<type>`, `action_values.<type>`, `cost_per_action.<type>` | any action type, for example `actions.link_click` |
| `daily_budget`, `lifetime_budget`, `budget` | object fields, in major currency units |
| `age_hours`, `age_days`, `hours_since_update` | object fields |

**Results and double counting.** Meta reports one purchase under several
action types (`omni_purchase`, `purchase`,
`offsite_conversion.fb_pixel_purchase`). Each alias lists its types in
priority order and uses the first one present. The types are never added
together. `result` can be an alias, `action:<action_type>` or a list of
types.

**Missing values.** CPA with zero results, CTR with zero impressions, or a
division by zero all give a missing value. Any comparison with a missing value
is false, so `cpa > 500` never fires on an ad set without conversions. Use
`spend > X and purchases == 0` for that case, or `coalesce(cpa, 0)` and
`missing(cpa)`. Objects with no insights row in the window have zero counters.

### Actions

| Action | Form | Notes |
|---|---|---|
| pause | `pause` | `POST /{id}` with `status=PAUSED`; skipped if already paused |
| notify | `notify`, `notify: telegram`, `notify: {channels: [..], message: ".."}` | message templates use `{name}`, `{spend}`, `{cpa}`, `{currency}`, `{window}`, `{actions}`, `{values}`, any metric or variable, with format specs such as `{spend:,.0f}` |
| scale_budget | `scale_budget: 15` or `{percent: -20, min: 500, max: 20000, budget: auto}` | caps are in major units (taka, not poisha); `budget: auto/daily/lifetime`; skipped when the object has no budget of its own (for example, an ad set under a campaign budget) |
| label | `label: fatigued` | attaches an ad label by name; Meta creates it if needed |

Rules run in file order. If an earlier rule pauses an object, a later rule in
the same run sees it as paused, in dry runs too.

## Configuration

See [`examples/guard.example.yaml`](examples/guard.example.yaml) for a fully
commented file. `${VAR}` and `${VAR:-default}` are expanded everywhere.

| Key | Default | Meaning |
|---|---|---|
| `account.id` | `$META_AD_ACCOUNT_ID` | `act_123...` or the bare number |
| `account.currency` | detected | checked against the account and against currency suffixes in rules |
| `account.timezone` | detected | IANA name; windows use it |
| `api.version` | `v27.0` | Graph API version, for example `v27.0` |
| `api.access_token_env` | `META_ACCESS_TOKEN` | env var holding the token |
| `api.app_secret_env` | none | env var holding the app secret (`appsecret_proof`) |
| `api.usage_threshold` | `85` | usage percentage at which calls slow down |
| `api.max_retries`, `api.timeout`, `api.max_wait` | `5`, `30`, `900` | retry count, seconds per request, longest pause |
| `insights.mode` | `auto` | `sync`, `async`, or `auto` (async above `async_threshold_days`) |
| `insights.async_threshold_days` | `30` | |
| `insights.poll_interval`, `insights.poll_timeout` | `5`, `1800` | seconds |
| `insights.attribution_windows` | account default | for example `[7d_click, 1d_view]` |
| `state.path` | in memory | `.json`, `.db`, `.sqlite` or `.sqlite3` |
| `state.cooldown_scope` | `rule` | `rule`: per rule and object; `object`: one action per object across rules |
| `audit.path`, `audit.include_dry_run` | none, `true` | JSON Lines file |
| `safety.max_actions_per_run` | `25` | matches beyond it are reported as `capped` |
| `safety.max_budget_increase_pct`, `safety.max_budget_decrease_pct` | `50`, `50` | upper bounds for `scale_budget.percent` |
| `defaults.window`, `defaults.cooldown`, `defaults.result`, `defaults.effective_status`, `defaults.level` | `3d`, `6h`, `purchases`, `[ACTIVE]`, `adset` | rule defaults |
| `vars` | none | numbers usable in every rule |
| `notify.mode` | `digest` | or `each` |

Relative paths are resolved against the config file's directory.

### Notifiers

```yaml
notifiers:
  telegram:
    type: telegram
    bot_token_env: TELEGRAM_BOT_TOKEN
    chat_id: "-1001234567890"
  slack:
    type: webhook
    url_env: SLACK_WEBHOOK_URL       # body {"text": "..."}; field: content for Discord
  email:
    type: email
    host: smtp.example.com
    port: 587
    security: starttls               # starttls | ssl | none
    username_env: SMTP_USERNAME
    password_env: SMTP_PASSWORD
    from: ads-alerts@example.com
    to: [owner@example.com]
```

Long Telegram and webhook messages are split into several messages. A failed
notifier is reported as an error and doesn't stop the run.

## Rate limits

After every response the client reads the usage headers:

- `X-Business-Use-Case-Usage`: per business object, `call_count`,
  `total_cputime` and `total_time` as percentages, plus
  `estimated_time_to_regain_access` in minutes.
- `X-Ad-Account-Usage`: `acc_id_util_pct` and `reset_time_duration`.
- `X-FB-Ads-Insights-Throttle`: `app_id_util_pct` and `acc_id_util_pct`.
- `X-App-Usage`: platform limits.

The client pauses when the highest value passes `api.usage_threshold`, and
pauses longer as usage nears 100 %. A non-zero
`estimated_time_to_regain_access` always takes priority. Throttling errors (4,
17, 32, 613 and 80000-80014) are retried with that wait, or with exponential
back-off and jitter when there is no hint. Two kinds of error are raised
straight away instead of retried: subcodes for change quotas (613/1487632, an
ad set budget changed more than 4 times in an hour; 17/1885172; 613/1487225)
and authentication errors. Because of the budget quota, `validate` warns when
a `scale_budget` rule has a cooldown under one hour.

## Using it as a library

```python
from meta_ads_guard.cli import build_api
from meta_ads_guard.config import load_config
from meta_ads_guard.engine import Engine

config = load_config("guard.yaml")
report = Engine(config, build_api(config)).run()   # dry run
for match in report.matches:
    print(match.rule.name, match.obj.name, [r.status for r in match.results])
```

`GraphClient` takes any object with a `request()` method as its transport, so
you can plug in your own HTTP stack or a fake. See `tests/conftest.py`.

## Examples

- [`examples/guard.example.yaml`](examples/guard.example.yaml): annotated configuration with six rules
- [`examples/offline_demo.py`](examples/offline_demo.py): the engine against a fake account, no network
- [`examples/library_usage.py`](examples/library_usage.py): library use
- [`examples/cron.example`](examples/cron.example): crontab entry
- [`examples/github-actions-schedule.yml`](examples/github-actions-schedule.yml): scheduled workflow with a cached state file
- [`examples/.env.example`](examples/.env.example): environment variables

## Testing

```console
$ pip install -e ".[dev]"
$ ruff check .
$ pytest
```

The suite runs against a mocked HTTP layer and never touches the network. It
covers the expression parser, windows, metrics, rate-limit headers, retries,
pagination, async report jobs, every action, cooldowns in both stores,
notifiers and the CLI.

## Limitations

- **No currency conversion.** Amounts in rules are in the account currency,
  and a rule that writes `USD` on a BDT account is rejected.
- **Attribution and delays.** Insights refresh roughly every 15 minutes and can
  change for days, especially conversions. Rules on `today` see partial data.
  Use windows and thresholds that tolerate this.
- **One account per config.** To guard several accounts, run one config per
  account.
- **Budgets live in one place.** With campaign budget optimisation the budget
  is on the campaign, so `scale_budget` on its ad sets is skipped. Use
  `level: campaign` instead.
- **Labels on ad sets** are sent as the `adlabels` parameter on the ad set
  itself. Campaigns and ads use their `/adlabels` edge. Check the result in
  Ads Manager after your first `--apply`.
- Frequency and reach are not additive. When several rows are merged, reach is
  summed, so treat frequency as an approximation in that case.
- Only the ad set and campaign budget fields are changed. Bids, schedules and
  targeting are out of scope.
- This is not an official Meta product. Read the plan before using `--apply`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues privately as
described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Saizul Amin

## Docker image

A multi-arch image (linux/amd64, linux/arm64) is published to GitHub Packages:

```bash
docker pull ghcr.io/saizul/meta-ads-guard:latest
docker run --rm -v "$PWD:/work" -e META_ACCESS_TOKEN ghcr.io/saizul/meta-ads-guard:latest validate -c guard.yaml
docker run --rm -v "$PWD:/work" --env-file .env ghcr.io/saizul/meta-ads-guard:latest run -c guard.yaml
```

Mount the folder that holds `guard.yaml` at `/work`. The state file and the
audit log are written there too, so cooldowns survive between container runs.
Pass secrets with `-e` or `--env-file`, never bake them into an image.
