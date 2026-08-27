# Discord setup

Bot Rooms uses the Discord identities already configured for participating
Hermes profiles. It never stores bot tokens in room configuration.

## One bot per profile

For every participating profile:

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable Message Content intent.
3. Enable Server Members intent when Hermes authorization rules resolve
   usernames or roles.
4. Invite the bot with the `bot` and `applications.commands` scopes.
5. Store its token through the standard Discord setup flow for that installed
   Hermes version, selecting the intended profile.

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
