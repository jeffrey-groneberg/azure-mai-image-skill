# Azure MAI Image

Agent skill for generating and editing images with MAI-Image-2.7 in Azure Foundry, with an optional MAI-Image-2.6 alternate. It uses Azure CLI authentication, keeps tokens in memory, and requires Python 3.10+. MAI-Image-2.7 generates up to 4.19 MP (2048x2048, or 2704x1520 at 16:9) and can return PNGs with a transparent background.

```bash
# GitHub Copilot
gh skill install jeffrey-groneberg/azure-mai-image-skill azure-mai-image --scope user --agent github-copilot

# Claude Code
gh skill install jeffrey-groneberg/azure-mai-image-skill azure-mai-image --scope user --agent claude-code
```

Edit the installed `config.json` for your Azure deployment (`~/.copilot/skills/azure-mai-image/` or `~/.claude/skills/azure-mai-image/`), run `az login --tenant <tenant-id>`, then ask your agent to generate or revise an image. Billable Azure requests may be made.

## Deployments and rate limit

The `config.json` template expects a MAI-Image-2.7 deployment named `mai-image-27` (version `2026-11-02`, preview) as the default, and an optional MAI-Image-2.6 deployment named `mai-image` (version `2026-07-31`) for the 2.6-only options. Rename or delete the entries to match your resource. An existing MAI-Image-2.6 configuration keeps working: the helper uses the deployment that `deployment` names.

Each capacity unit of a MAI image deployment allows 1 request per minute. The template assumes capacity 10, so 10 requests per minute per deployment: set `capacity` and `requests_per_minute` to your deployments' real values, and check your subscription's quota first. The [Azure CLI reference](skills/azure-mai-image/references/azure-cli.md) has the commands to check quota and create both deployments.

## License

MIT
