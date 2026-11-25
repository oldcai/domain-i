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
| `--tlds com,io,dev` | TLDs to check in availability mode, for this run only |

Exit codes: `0` normal, `2` bad arguments, `1` when every network lookup failed.

### Lookup mode: input with a suffix

```
$ domain-i github.com
github.com  REGISTERED
Created    2007-10-09
Updated    2026-09-07
Expires    2028-10-09 (683 days left)
Privacy    unknown
Registrar  MarkMonitor Inc.
DNS        dns1.p08.nsone.net, dns2.p08.nsone.net, ns-1283.awsdns-32.org, ...
Rank       #34 (tranco)
```

* Dates are normalised to `YYYY-MM-DD`; `Expires` shows the days left.
* `Privacy` is `yes (<what matched>)` when the registrant is hidden behind a
  privacy/proxy service or redacted, `no (<organisation>)` when a real holder is
  published, and `unknown` when the registry publishes no registrant at all.
* Registries without a port-43 WHOIS service (for example `.app`, `.dev`, `.uk`
  on some clients) fall back to [RDAP](https://about.rdap.org/).
* A domain is only reported as **not registered** on positive evidence (a WHOIS
  "not found" answer or an RDAP 404). Timeouts or empty answers are reported as
  **UNKNOWN**. In both cases domain-i continues in availability mode with the
  domain as the first row.

Anything like `https://www.Example.com/path` is cleaned to `example.com`.

### Availability mode: input without a suffix

```
$ domain-i password manager
Domain                Status     Price
passwordmanager.com   taken      -
passwordmanager.net   taken      -
passwordmanager.org   taken      -
passwordmanager.app   taken      -
password-manager.com  taken      -
password-manager.net  taken      -
password-manager.org  taken      -
password-manager.app  available  -

All plain names are taken, so hyphenated variants were checked too.
Backend: Spaceship
```

* Words come from spaces or hyphens (`foo bar`, `foo-bar`); a single word is
  split with wordninja (`foobar` -> `foo bar`).
* The plain name is checked first. Hyphenated variants are checked **only if every
  plain name is confirmed taken**. A single word that cannot be split gets a
  hint to retry with spaces.
* `Price` is only filled in for premium names that are available (Spaceship
  backend only).
* Status colours: green `available`, yellow `taken`, dim `unknown`.

## Configuration

Everything is optional. domain-i reads one `KEY=VALUE` file
(`export KEY=VALUE` and quoted values are fine):

* `$DOMAIN_I_ENV` if set, otherwise
* `~/.config/domain-i/env` (respects `$XDG_CONFIG_HOME`).

### Spaceship credentials (optional, gives premium prices)

Without credentials, availability is checked over RDAP. With a
[Spaceship API key](https://www.spaceship.com/application/api-manager/) (needs
the *domains:read* scope) all names are checked in one request and premium
prices are shown. Credentials are read from, in order:

1. environment variables `SPACESHIP_API_KEY` and `SPACESHIP_API_SECRET` (both set);
2. the config file above, same variable names.

```
# ~/.config/domain-i/env
SPACESHIP_API_KEY=...
SPACESHIP_API_SECRET=...
```

If the Spaceship call fails, domain-i falls back to RDAP and says so under the
table. To force RDAP for one run: `env -u SPACESHIP_API_KEY DOMAIN_I_ENV=/dev/null domain-i foo`.

### TLD list

Default: `com,net,org,app`, shown in that order. Override, highest priority first:

1. `--tlds com,io,dev`
2. environment variable `DOMAIN_I_TLDS="io dev"`
3. `DOMAIN_I_TLDS=com,io,dev` in the config file
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
