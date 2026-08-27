# Discord Bot Rooms installed

Before configuring a room, verify this Hermes build:

```bash
hermes botrooms doctor --pre-install
```

Then run the guided setup:

```bash
hermes botrooms setup --restart
```

Bot tokens remain in each profile's existing Hermes credential store. Never
paste them into a room configuration or an agent conversation.
