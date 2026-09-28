# Azure CLI reference

Configure `config.json`, then sign in:

```bash
az login --tenant <tenant-id>
```

The helper requests a short-lived token for
`https://cognitiveservices.azure.com`, keeps it in memory, and calls:

```text
https://<account-name>.services.ai.azure.com/mai/v1/images/generations
```

The Azure identity needs inference access to the Foundry resource. API-key
authentication is not used. Never print or persist access tokens, enable shell
tracing, or use `az --debug`.

Useful read-only checks:

```bash
az account get-access-token \
  --subscription <subscription-id> \
  --resource https://cognitiveservices.azure.com \
  --query '{tenant:tenant,subscription:subscription,expiresOn:expiresOn}' \
  --output json --only-show-errors

az cognitiveservices account deployment show \
  --subscription <subscription-id> \
  --resource-group <resource-group> \
  --name <account-name> \
  --deployment-name <deployment-name> \
  --output json --only-show-errors
```

Official documentation:

- https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-mai-image
- https://learn.microsoft.com/azure/foundry/foundry-models/how-to/configure-entra-id
- https://learn.microsoft.com/cli/azure/account#az-account-get-access-token
