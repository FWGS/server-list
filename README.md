# Xash3D static HTTP server list

Curated lists of public Xash3D / GoldSrc servers, probed on a schedule and
published as static text files via GitHub Pages. The engine consumes them
through the `masterstatic` directive in `xashcomm.lst`. See
[`Documentation/protocol/http-server-list.md`](https://github.com/FWGS/xash3d-fwgs/blob/master/Documentation/protocol/http-server-list.md)
in xash3d-fwgs for the wire format and engine integration.

## Layout

```
servers/
  <gamedir>.toml      # hand-curated source list, one file per gamedir
scripts/
  probe.py            # probes every source entry, emits the publishable tree
.github/workflows/
  publish.yml         # nightly: install xash3d-query, run probe.py, deploy Pages
```

`output/` is generated; it is not committed.

## Adding a server

1. Edit (or create) `servers/<gamedir>.toml`.
2. Add a `[[server]]` table:

   ```toml
   [[server]]
   address  = "203.0.113.7:27015"
   protocol = 49        # 49 = Xash, 48 = GoldSrc
   contact  = "admin@example.com"   # required for new entries; never published
   # host   = "Cool Deathmatch"     # optional informational note
   ```

3. Open a PR. The nightly workflow will start probing it and, once it answers,
   include it in the published list.

`contact` and any other source-side keys are stripped from the published
output. They live only in this repository, so the contact address is visible
to maintainers but never to end users.

### Contribution policy

- **No spammy, abusive, or hostile servers.** Reskinned ad farms, servers
  that run hostile scripts against connecting clients, fake-info hosts,
  content meant to harass players: all out. Maintainers may reject or remove
  any entry without further discussion. PRs from accounts that have done
  this before will be closed on sight.
- **`contact` is mandatory for every new entry.** One of:
  - email, e.g. `admin@example.com`
  - Discord, as `discord:username` or an invite to a channel where the admin
    can be reached. If the invite expires, or a server-list maintainer gets
    banned from that Discord, the entry is removed (or commented out as a
    soft disable). An unreachable contact is no contact.
  - Telegram, as `telegram:@username` or a group link. Same rule as Discord:
    if the link expires, a maintainer gets banned, or the `@username` is
    unreachable, the entry is removed or commented out.
  Multiple contacts in one line are fine:
  `contact = "admin@example.com, discord:foo"`. We won't publish the value,
  but maintainers need a way to reach you when a server starts misbehaving
  or its address changes.

  **Please don't ignore messages on the channel you submit.** Contacts will
  not be a chatty inbox. You'll only ever hear from us for two reasons: an
  abuse report against your server, or an automated reminder that your
  engine/mod build is outdated. Filtering those out gets your entry removed,
  same as an unreachable address.
- **Legacy entries.** The servers populated in the initial commit, seeded
  from the old UDP master lists, have no `contact` field because we don't
  know who to ask. They are grandfathered in. Any new PR that adds an entry
  without `contact` will be asked to add one before merge. If you recognize
  a legacy entry as your own, please open a PR adding the contact.
- **One PR per operator.** Bundle all the servers you run into a single PR
  rather than spreading them across many. It makes review and future contact
  easier.

## Liveness, retries, and grace

Each source address is queried up to `--tries` times (default 4) with a
short back-off between attempts. A server is published if any attempt
succeeded, or if `state.json` says it answered within the last
`--grace-hours` (default 48). That smooths over spurious failures: a
single missed run does not drop a server from the list, but an address
that has been silent for two days does.

`state.json` lives at the root of the published tree (next to `index.html`).
The workflow fetches the previous copy from the live site before each run
and writes a fresh one as part of the publish. Addresses that have been
removed from `servers/` and unseen for a month are pruned automatically.

## Local dry-run

```sh
# Get xash3d-query somewhere on $PATH
gh release download continuous --repo FWGS/xash3d-master \
    --pattern '*x86_64-unknown-linux-gnu*'
tar --use-compress-program=unzstd -xf xash3d-master-*.tar.zst

# Probe once and write to output/
./scripts/probe.py --query ./xash3d-query --output ./output
```

`output/state.json` carries last-seen timestamps between runs; delete it to
re-probe everything from scratch.
