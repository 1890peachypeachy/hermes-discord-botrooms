# Instructions for coding and installation agents

Follow this repository guidance and `AGENT_INSTALL.md` before changing a
user's Hermes installation.

## Non-negotiable rules

- Discover the Hermes root, profiles, operating system, plugin revision,
  gateway services, and Discord configuration. Never assume names or paths.
- Never ask the user to paste a Discord token into chat.
- Never print, log, diff, serialize, or copy token values. Report only whether
  a credential exists and which bot identity Discord returns.
- Never enable an allow-all Discord authorization setting.
- Never edit a profile's `SOUL.md`, model, memory, skills, or normal messaging
  configuration as part of this installation.
- Never install an unpinned plugin revision for an unattended user.
- Run a dry run and show the proposed profiles, controller, config path,
  install revision, and gateway restarts before making changes.
- Restart only the selected profiles' gateways.
- A running process is not acceptance. Verify the loaded plugin revision,
  Hermes home, Discord identity, channel access, and one real room message.
- Stop if the compatibility preflight fails. The only private-beta exception
  is the exact, clean-source patch procedure in `AGENT_INSTALL.md`, after the
  user explicitly approves it. Never patch a different Hermes revision or an
  installed runtime whose source and process ownership are not verified.

## Repository changes

- Keep the plugin independent from developer-specific machines and profiles.
- Tests must use temporary Hermes homes and invented profile names.
- Preserve the MIT and upstream Hermes attribution.
- Use the smallest compatible extension surface. Do not copy the complete
  built-in Discord adapter into this repository.
