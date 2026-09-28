# Azure MAI Image

Agent skill for generating and editing images with MAI-Image in Azure Foundry. It uses Azure CLI authentication, keeps tokens in memory, and requires Python 3.10+.

```bash
# GitHub Copilot
gh skill install jeffrey-groneberg/azure-mai-image-skill azure-mai-image --scope user --agent github-copilot

# Claude Code
gh skill install jeffrey-groneberg/azure-mai-image-skill azure-mai-image --scope user --agent claude-code
```

Edit the installed `config.json` for your Azure deployment (`~/.copilot/skills/azure-mai-image/` or `~/.claude/skills/azure-mai-image/`), run `az login --tenant <tenant-id>`, then ask your agent to generate or revise an image. Billable Azure requests may be made.

## License

MIT
