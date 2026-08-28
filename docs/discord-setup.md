# Discord setup

Bot Rooms uses the Discord identities already configured for participating
Hermes profiles. It never stores bot tokens in room configuration.

Choose a dedicated channel for the room. Every top-level human message in that
channel creates a new Bot Room thread with fresh context. Follow-up messages
belong inside the thread they should continue.

An authorized Discord server administrator must be available to create or
invite the bots and change channel permissions. Human operators who use Bot
Rooms commands also need Discord's Use Application Commands permission in the
room channel.

## One bot per profile

For every participating profile:

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable Message Content intent.
3. Enable Server Members intent when Hermes authorization rules resolve
   usernames or roles.
4. Invite the bot with the `bot` and `applications.commands` scopes.
5. Start that profile's Hermes setup, choose Discord when prompted, and store
   the token there:

   ```bash
   hermes --profile <profile-name> setup
   ```

   Replace `<profile-name>` with an existing profile. Prompt wording can vary
   by Hermes version.

Never paste a token into this repository, room configuration, an issue, or an
agent conversation.

## Channel permissions

Every participating bot needs:

- View Channel
- Send Messages
- Read Message History
- Send Messages in Threads
- Attach Files

The controller bot also needs Create Public Threads. It receives human room
messages and creates the Discord thread; member bots post their own answers
inside it.

Inside a room, `@profile-name` or a configured member handle limits the next
turn to that member. Choosing the member bot with Discord's mention picker is
also supported. Bot responses suppress outbound Discord pings even when their
text contains a routing handle.

`/room-status`, `/stop`, `/room-answer`, and `/room-approval` are native Discord
slash commands registered by the participating bot applications. The adapter
also recognizes ordinary text messages containing only `/room-status` or
`/stop`, but the native commands are the normal operator path.

## Values needed by setup

Enable Discord Developer Mode, then copy:

- the server ID
- the channel ID

You will also choose:

- a short room ID
- 2–6 Hermes profiles
- one selected profile as controller

Profile names and Discord numeric IDs are safe to give an installing agent.
Tokens are not.

After installation, `hermes botrooms doctor --live --json` verifies bot
identities and channel read access without sending a message. Sending and
thread creation still require the real acceptance test in
[Operations and rollback](operations.md#live-acceptance-checklist).
