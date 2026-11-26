# This project is now Tokenwell

Claude Unlimited has been renamed **Tokenwell** and moved to a new repository:

**https://github.com/Mabuti/tokenwell**

This repository is archived and receives no further updates. Its history and releases (up to
`v1.3.1.11`) are kept here as a record.

## Upgrading an existing install

Install once from the new location:

```bash
curl -fsSL https://raw.githubusercontent.com/Mabuti/tokenwell/main/install.sh | bash
```

On Windows, use the PowerShell one-liner in the Tokenwell README.

The installer backs up `~/.claude-unlimited` first, then moves your data to `~/.tokenwell`, keeps your
accounts and usage history, and replaces the old login service with the new one. The `cu` and
`claude-unlimited` commands keep working alongside the new `tokenwell` and `tw` commands. See
"Upgrading from Claude Unlimited" in the Tokenwell README for the details.

The built-in updater of older versions looks for releases in this repository, so it will not find
Tokenwell releases. Reinstall once with the command above.

## License

MIT. Tokenwell is derived from
[DevDock-AI/claude-unlimited](https://github.com/DevDock-AI/claude-unlimited).
