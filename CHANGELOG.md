# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-12

### Added

- YAML rules over Marketing API insights for campaigns, ad sets and ads, with
  a safe expression language (`and`/`or`/`not`, arithmetic, `×`, chained
  comparisons, currency suffixes, `min`/`max`/`abs`/`coalesce`/`round`/`missing`).
- Lookback windows per rule (`3d`, `last_7d`, `today`, `yesterday`,
  `this_month`, `last_month`, `lifetime`), per metric (`spend[today]`) and as a
  trailing clause (`... in last 3 days`), computed in the ad account time zone.
- Metrics: spend, impressions, reach, clicks, link clicks, CTR, link CTR, CPC,
  CPM, frequency, results and cost per result for a configurable result type,
  purchases, leads, messaging conversations, add to cart, checkout,
  registrations, conversion value, ROAS, raw `actions.<type>` lookups and
  object fields (budget, age).
- Actions: pause, notify, scale budget by a percentage within min/max caps,
  and label. Dry run by default; `--apply` to act.
- Cooldowns per rule and object (or per object across rules) in a JSON or
  SQLite state file, and a JSON Lines audit log.
- Safety limits: maximum actions per run and maximum budget change per action.
- Graph API client with a pinned API version, cursor pagination,
  `appsecret_proof`, retries for transient errors, and back-off driven by the
  `X-Business-Use-Case-Usage`, `X-Ad-Account-Usage`,
  `X-FB-Ads-Insights-Throttle` and `X-App-Usage` headers and throttling error
  codes 4, 17, 32, 613 and 80000-80014.
- Asynchronous insights report jobs for long ranges, with polling and timeout.
- Notifiers: Telegram, Slack-compatible webhooks and SMTP email, as a digest
  or one message per match.
- CLI commands `run`, `validate`, `insights`, `cooldowns`, `audit` and
  `metrics`; Docker image.

[Unreleased]: https://github.com/saizul/meta-ads-guard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/meta-ads-guard/releases/tag/v0.1.0
