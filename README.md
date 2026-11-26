# domain-i

A small command line tool that answers the two questions you usually have when
you type `whois`:

* **Is this domain registered, and what are the essentials?** A six-line summary
  instead of a page of WHOIS boilerplate.
* **Is this name free anywhere?** Check a name across a few TLDs at once,
  including hyphenated variants.

```
pip install domain-i          # or: uv tool install domain-i
alias i=domain-i              # fish, zsh and bash all accept this line
```

Requires Python 3.10+ and the system `whois` client. The only runtime
dependency is [`wordninja`](https://github.com/keredson/wordninja) (used to split
`passwordmanager` into `password manager`).

## Usage

```
domain-i [-v] [--no-metrics] [--color] [--tlds LIST] <domain | name | words...>
```

| Option | Meaning |
| --- | --- |
| `-v`, `--verbose` | print the raw WHOIS output (and RDAP JSON when RDAP was used) before the summary |
| `--no-metrics` | skip the Rank / DR lookups for registered domains |
| `--color` | force coloured output (also `FORCE_COLOR=1`) |
| `--tlds com,io,dev` | TLDs for the availability table, for this run only |

Exit codes: `0` normal, `2` bad arguments, `1` when every network lookup failed.

### Lookup mode: input with a suffix

```
$ domain-i github.com
github.com  REGISTERED
Created    2007-10-09
Updated    2026-09-07
Expires    2028-10-09 (683 days left)
Privacy    yes (REDACTED FOR PRIVACY)
Registrar  MarkMonitor Inc.
DNS        dns1.p08.nsone.net, dns2.p08.nsone.net, ns-1283.awsdns-32.org, ...
Rank       #34 (tranco)

Domain      Status
github.com  taken   ← this
github.net  taken
github.org  taken
github.app  taken
Taken: 4/4

No hyphenated variant: 'github' could not be split into words. Retry with spaces to choose the split, e.g. domain-i git hub
Backend: Spaceship
```

* Dates are normalised to `YYYY-MM-DD`; `Expires` shows the days left.
* `Privacy` is `yes (<what matched>)` when the registrant is hidden behind a
  privacy/proxy service or redacted, `no (<organisation>)` when a real holder is
  published, and `unknown` when nobody publishes registrant data. When WHOIS
  cannot decide (thin registries such as `.com`, registrars without port 43),
  domain-i follows the registry's RDAP `related` link to the registrar's RDAP
  server and reads the registrant there, in the background.
* Fields the registry does not publish are omitted (except `Privacy`).
* Registries without a port-43 WHOIS service (for example `.app`, `.dev`, `.uk`
  on some clients) fall back to [RDAP](https://about.rdap.org/).
* A domain is only reported as **not registered** on positive evidence (a WHOIS
  "not found" answer or an RDAP 404). Timeouts or empty answers are reported as
  **UNKNOWN**.

* After the summary comes the same table as availability mode (below) for
  the name in your configured TLDs, so you see at a glance how taken the name
  is. The looked-up domain keeps its place in the list (or comes first if its
  TLD is not in the list), is marked `← this`, and always shows the status of
  the lookup above. That check starts in parallel with WHOIS, so it adds little
  time; if it fails, a one-line note replaces the table and the exit code is
  unaffected.

Anything like `https://www.Example.com/path` is cleaned to `example.com`.

### Availability mode: input without a suffix

```
$ domain-i passwordmanager
Domain                Status
passwordmanager.com   taken
passwordmanager.net   taken
passwordmanager.org   taken
passwordmanager.app   taken
password-manager.com  taken
password-manager.net  taken
password-manager.org  taken
password-manager.app  taken
Taken: 4/4

All plain names are taken, so hyphenated variants were checked too.
Backend: Spaceship

$ domain-i teleprompter web
Domain                Status
teleprompterweb.com   taken
teleprompterweb.net   available
teleprompterweb.org   available
teleprompterweb.app   available
teleprompter-web.com  taken
Taken: 1/4

teleprompterweb.com is taken, so teleprompter-web.com was checked too.
Backend: Spaceship
```

* Words come from spaces or hyphens (`foo bar`, `foo-bar`); a single word is
  split with wordninja (`foobar` -> `foo bar`).
* The plain name is checked first in every configured TLD. The **primary TLD**
  is the first one in the list (`.com` with the default `com,net,org,app`;
  `io` for `--tlds io,com`). Hyphenated names are then checked like this:
  * every plain name confirmed taken -> the hyphenated name in every TLD;
  * otherwise, if the plain name in the primary TLD is confirmed taken -> the
    hyphenated name in the primary TLD only (e.g. `password-manager.com`);
  * otherwise (primary TLD available or unknown) -> no hyphenated names.

  A single word that cannot be split gets a hint to retry with spaces instead.
  `Taken: n/m` counts only the plain name in the configured TLDs (`(k unknown)`
  is added when some could not be checked).
* **Traffic / DR columns**: rows whose status is `taken` also get the metrics
  from your metrics commands. In a terminal the table appears immediately with
  a dim `...` placeholder in those cells and is redrawn in place as values
  arrive (redrawn at most ~10x/second, finishes within the metrics budget);
  when stdout is a pipe nothing is animated and only the final table is
  printed. Columns with no value at all are dropped from the final output, so
  with no metrics commands (or `--no-metrics`) the table looks exactly as
  before. Traffic in the table is the compact form (`745.6M`, no `/mo` or
  month); the full form stays in the lookup summary. At most 4 commands run at
  once, and the domain you looked up reuses the summary's numbers instead of
  being queried again.
* Status colours: green `available`, yellow `taken`, dim `unknown`. Metric
  rows: cyan `Traffic`, magenta `DR`, blue `Rank`.

## Configuration

Everything is optional. Settings live in `KEY=VALUE` files
(`export KEY=VALUE`, quotes and `#` comments are fine):

* every `*.env` file in `~/.config/domain-i/` (respects `$XDG_CONFIG_HOME`),
  read in file-name order, later files overriding earlier ones; or
* only the single file named by `$DOMAIN_I_ENV`, when that variable is set.

A convenient layout keeps secrets and preferences apart, e.g. a
`credentials.env` (possibly a symlink to wherever you keep secrets) and a
`config.env` for options:

```
~/.config/domain-i/
  config.env        # DOMAIN_I_TLDS=com,net,org,app
  credentials.env   # SPACESHIP_API_KEY=... / SPACESHIP_API_SECRET=... / AHREFS_API_KEY=...
```

### Spaceship credentials (optional, gives premium prices)

Without credentials, availability is checked over RDAP. With a
[Spaceship API key](https://www.spaceship.com/application/api-manager/) (needs
the *domains:read* scope) all names are checked in one request and premium
prices are shown. Credentials are read from, in order:

1. environment variables `SPACESHIP_API_KEY` and `SPACESHIP_API_SECRET` (both set);
2. the config files above, same variable names.

If the Spaceship call fails, domain-i falls back to RDAP and says so under the
table. To force RDAP for one run: `env -u SPACESHIP_API_KEY DOMAIN_I_ENV=/dev/null domain-i foo`.

### TLD list

Default: `com,net,org,app`, shown in that order. Override, highest priority first:

1. `--tlds com,io,dev`
2. environment variable `DOMAIN_I_TLDS="io dev"`
3. `DOMAIN_I_TLDS=com,io,dev` in a config file (e.g. `config.env`)
4. the built-in default

Commas or spaces, optional leading dots and any case are accepted (`.COM io`),
duplicates are dropped and multi-label suffixes such as `co.uk` work. If the
value is empty or entirely invalid, the default is used and a warning is printed
to stderr. TLDs without a known RDAP server are reported as `unknown` unless
Spaceship answers for them.

### Rank and DR (registered domains only)

Fetched in parallel with WHOIS, at most ~8 seconds, and silently omitted when
unavailable (`--no-metrics` turns them off):

* `Rank #N (tranco)` - the [Tranco](https://tranco-list.eu/) research top-sites
  rank. Free, no key. It is a popularity rank, not a visit count.
* `DR N (ahrefs)` - Ahrefs Domain Rating via the free
  [`domain-rating-free`](https://docs.ahrefs.com/) endpoint. Only shown when
  `AHREFS_API_KEY` is set (environment or config file). Domain Rating by Ahrefs.

### Bring your own metrics source

If you have another source of traffic or authority numbers, point
`DOMAIN_I_METRICS_CMD` at a command (environment variable or any config
`*.env` file; the environment wins). For registered domains domain-i runs it
in the background with the domain appended as the last argument, without a
shell (`~` in arguments is expanded), and waits at most 8 seconds.

To combine several sources, add more variables named
`DOMAIN_I_METRICS_CMD_<NAME>` (for example `DOMAIN_I_METRICS_CMD_TRAFFIC` and
`DOMAIN_I_METRICS_CMD_DR`). All configured commands run concurrently. For each
field, the first command that returns a usable value wins, in this order: the
bare `DOMAIN_I_METRICS_CMD` first, then the others sorted by name. An
environment variable overrides the config entry of the same name, and an empty
value disables it.

The command prints one JSON object on stdout. Every field is optional and
unknown fields are ignored:

| Field | Meaning |
| --- | --- |
| `traffic` | estimated monthly visits (number) |
| `traffic_month` | the month the estimate is for, e.g. `"2026-10"` |
| `dr` | an authority score from 0 to 100 (number) |
| `source` | name shown in brackets; defaults to `custom` |

A made-up example, `~/bin/acme-metrics`:

```python
#!/usr/bin/env python3
import json, sys

domain = sys.argv[-1]
# ... look the domain up in your own data ...
print(json.dumps({"traffic": 1234567, "traffic_month": "2026-10", "dr": 42, "source": "acme"}))
```

```
# ~/.config/domain-i/config.env
DOMAIN_I_METRICS_CMD=~/bin/acme-metrics
# optional second source, e.g. one that only knows an authority score:
DOMAIN_I_METRICS_CMD_DR=~/bin/other-authority
```

```
Traffic    1.2M/mo (acme, 2026-10)
DR         42 (acme)
Rank       #34 (tranco)
```

Rows appear in the order Traffic, DR, Rank. The same numbers also fill the
Traffic / DR columns of the availability table for rows that are taken. A `dr` from the command replaces
the Ahrefs row. Missing, zero or non-numeric values, a non-zero exit status, a
timeout or output that is not a JSON object simply leave the rows out; run with
`-v` to see why on stderr. `--no-metrics` skips every command.

### Colour

Colour is used only when stdout is a terminal and `NO_COLOR` is not set.
`--color` or `FORCE_COLOR=1` forces it. Without colour the status words
(`REGISTERED`, `NOT REGISTERED`, `UNKNOWN`, `available`, `taken`, `unknown`)
still carry the meaning.

### Cache

The IANA RDAP bootstrap file is cached for 7 days in `~/.cache/domain-i/`
(respects `$XDG_CACHE_HOME`).

## Development

```
uv sync
uv run pytest
uv run mypy
```

Tests never touch the network or your real config/cache: WHOIS and RDAP
parsing run against recorded samples in `tests/fixtures/`.

## License

MIT
