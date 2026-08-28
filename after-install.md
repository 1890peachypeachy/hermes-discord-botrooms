# Discord Bot Rooms installed

Before configuring a room, verify this Hermes build:

```bash
hermes botrooms doctor --pre-install
```

Then run the guided setup:

```bash
hermes botrooms setup --restart
```

Gateway restart is the default; the flag is included here to make it explicit.

If this replaces Bot Rooms v0.2, read the
[v0.3.0 beta 1 upgrade notes](docs/releases/v0.3.0-beta.1.md) before restarting
the participating gateways.

Bot tokens remain in each profile's existing Hermes credential store. Never
paste them into a room configuration or an agent conversation.
