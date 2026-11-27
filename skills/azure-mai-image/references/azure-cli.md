# Azure CLI and token reference

Replace the `<...>` placeholders with the values from your `config.json`.

## Sign in and access

```bash
az login --tenant <tenant-id>
```

The helper requests a short-lived token for `https://cognitiveservices.azure.com`,
keeps it in memory, and calls:

```text
https://<account-name>.services.ai.azure.com/mai/v1/images/generations
https://<account-name>.services.ai.azure.com/mai/v1/images/edits
```

The signed-in identity needs an inference (data-plane) role on the Foundry
resource, such as Cognitive Services User. An Owner or Contributor control-plane
role alone does not grant inference access. API-key authentication is not used,
so the resource can have local (key) authentication disabled. Hosted agents
should use an approved managed identity or workload identity, not a copied
user token.

## Direct image generation with az rest

`az rest --resource` acquires and attaches the bearer token automatically.
Prefer this over putting a token in a command argument or environment variable.
Do not enable debug output or HTTP tracing.

The following is a billable image-generation request. Use a fresh response and
image filename so existing user files are not overwritten. For ordinary agent
usage, prefer `scripts/generate.py`, which handles prompts and file safety.

```bash
SKILL="$HOME/.copilot/skills/azure-mai-image"
SUBSCRIPTION="<subscription-id>"
ENDPOINT="https://<account-name>.services.ai.azure.com"

az rest \
  --subscription "$SUBSCRIPTION" \
  --method post \
  --resource https://cognitiveservices.azure.com \
  --url "$ENDPOINT/mai/v1/images/generations" \
  --headers Content-Type=application/json \
  --body @"$SKILL/references/request.example.json" \
  --output-file "$PWD/mai-response.json" \
  --output none \
  --only-show-errors

python3 "$SKILL/scripts/generate.py" \
  --response-json "$PWD/mai-response.json" \
  --output "$PWD/mai-image.png"
```

The response file contains base64 image data, not an authentication token.
Delete only this task's temporary response file once the image is saved, or
retain it if the user needs the API response.

The API payload uses `model` for the **deployment name** (`mai-image-27` in
[request.example.json](request.example.json)), not the catalog model name. No
`api-version` query is needed on `/mai/v1/images/generations`. `web_grounding`
and `auto_aspect_ratio` are documented only for MAI-Image-2.6, so the helper
sends them (default false) only to MAI-Image-2.6 deployments. For a transparent
background on MAI-Image-2.7, a generation request adds `"background": "transparent"`
(Learn's 2.7 example); the helper does this with `--transparent-background`.

## Explicit authentication token with Azure CLI

The exact command is:

```text
az account get-access-token --subscription <subscription-id> --resource https://cognitiveservices.azure.com --query accessToken --output tsv --only-show-errors
```

**Do not run that command with stdout displayed in the agent conversation.**
Capture stdout inside the HTTP client's process instead. The helper does this,
also validating the returned tenant and subscription:

```python
import subprocess

token = subprocess.run(
    [
        "az", "account", "get-access-token",
        "--subscription", "<subscription-id>",
        "--resource", "https://cognitiveservices.azure.com",
        "--query", "accessToken", "--output", "tsv", "--only-show-errors",
    ],
    check=True, capture_output=True, text=True, timeout=60,
).stdout.strip()

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
```

Never print `token`, persist it, put it in a subprocess's arguments, or copy it
to another agent/host. Get a current token for each generation invocation.
Azure CLI manages its own normal sign-in cache; this skill creates no
additional credential store.

To inspect sign-in without exposing a token:

```bash
az account get-access-token \
  --subscription <subscription-id> \
  --resource https://cognitiveservices.azure.com \
  --query '{tenant:tenant,subscription:subscription,expiresOn:expiresOn}' \
  --output json --only-show-errors
```

## Inspect deployments, rate limits and quota

These are read-only commands:

```bash
az cognitiveservices account deployment show \
  --subscription <subscription-id> \
  --resource-group <resource-group> \
  --name <account-name> \
  --deployment-name mai-image-27 \
  --query '{state:properties.provisioningState,model:properties.model,capacity:sku.capacity,rateLimits:properties.rateLimits}' \
  --output json --only-show-errors

az cognitiveservices account list-models \
  --subscription <subscription-id> \
  --resource-group <resource-group> \
  --name <account-name> \
  --query "[?starts_with(name,'MAI-Image')].{name:name,version:version,lifecycle:lifecycleStatus,deprecation:deprecation.inference}" \
  --output table --only-show-errors

az cognitiveservices usage list \
  --subscription <subscription-id> \
  --location <region> \
  --query "[?starts_with(name.value,'AIServices.GlobalStandard.MAI-Image')].{model:name.value,used:currentValue,limit:limit}" \
  --output table --only-show-errors
```

A deployment's `rateLimits` is its real limit: keep `capacity` and
`requests_per_minute` in `config.json` equal to it. The catalog lists 1 request
per minute per capacity unit for MAI-Image-2.7 and 2.6 on Global Standard. On
2026-11-27, deployments of both models with capacity 10 reported `rateLimits`
of 10 requests per 60 seconds; the API's 400 responses carried no rate-limit
headers. The catalog reported MAI-Image-2.7 and 2.7-Flash (`2026-11-02`) as
Preview with deprecation date 2027-02-17, MAI-Image-2.6 and 2.6-Flash
(`2026-07-31`) as GenerallyAvailable with 2027-05-01, and the MAI-Image-2.5
family with 2026-11-24.

The usage list shows the subscription's quota for each model in that region, in
capacity units (`used` of `limit`). A deployment's capacity must fit in the
remaining quota. Do not increase capacity or redeploy a duplicate without
checking quota and obtaining user approval.

## Create the resource and deployments

These commands change Azure resources. **Do not run them for normal image
requests**; creating or changing resources requires an explicit user request.

[account.json](account.json) is the ARM payload for a keyless Foundry resource:
set its `location`, `customSubDomainName` (the account name) and tags first.
`az rest` is used because Azure CLI's account-create command does not expose all
network/authentication properties; the resource must be keyless from its
creation, not patched afterward.

```bash
SKILL="$HOME/.copilot/skills/azure-mai-image"
SUBSCRIPTION="<subscription-id>"
RESOURCE_GROUP="<resource-group>"
ACCOUNT="<account-name>"
LOCATION="<region>"

az group create --subscription "$SUBSCRIPTION" \
  --name "$RESOURCE_GROUP" --location "$LOCATION"

az rest --subscription "$SUBSCRIPTION" --method put \
  --url "https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT?api-version=2026-05-01" \
  --body @"$SKILL/references/account.json"

az resource wait --subscription "$SUBSCRIPTION" \
  --ids "/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT" \
  --api-version 2026-05-01 --created --interval 5 --timeout 180
```

The default MAI-Image-2.7 deployment, with capacity 10 (10 requests per minute):

```bash
az cognitiveservices account deployment create \
  --subscription "$SUBSCRIPTION" \
  --resource-group "$RESOURCE_GROUP" --name "$ACCOUNT" \
  --deployment-name mai-image-27 \
  --model-name MAI-Image-2.7 --model-format Microsoft \
  --model-version 2026-11-02 \
  --sku-name GlobalStandard --sku-capacity 10
```

The optional MAI-Image-2.6 alternate, needed only for `--auto-aspect-ratio` and
`--web-grounding`:

```bash
az cognitiveservices account deployment create \
  --subscription "$SUBSCRIPTION" \
  --resource-group "$RESOURCE_GROUP" --name "$ACCOUNT" \
  --deployment-name mai-image \
  --model-name MAI-Image-2.6 --model-format Microsoft \
  --model-version 2026-07-31 \
  --sku-name GlobalStandard --sku-capacity 10
```

Lower `--sku-capacity` if your quota is smaller (Learn's example uses 1, which
allows 1 request per minute), and set `capacity` and `requests_per_minute` in
`config.json` to the same value. No access keys are needed or retrieved.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Azure CLI login/MFA failure | Have the user run `az login --tenant <tenant-id>`. Never collect their password or MFA code. |
| 401 | Check sign-in, the cognitive-services token resource, and inference data-plane permissions. |
| 403 | Inspect RBAC and networking. A scoped Cognitive Services User or equivalent inference role is needed; obtain approval before adding a role. |
| 404 | Verify the deployment name in `config.json` (`mai-image-27` by default, or `mai-image` for MAI-Image-2.6), the MAI `/mai/v1/` endpoint, and preview model retirement. A GET to the POST-only route is not a health check. |
| 400 dimension error | Each dimension >=768; total <=4,194,304 pixels on MAI-Image-2.7 (e.g. 2048x2048 or 2704x1520), <=2,359,296 on MAI-Image-2.6/2.6-Flash (e.g. 2048x1152) and <=1,048,576 on MAI-Image-2.5 models. Larger sizes such as 3840x2160 are rejected by the service (`unsupported_request_value`). The 2.7 limit is not on Learn yet; on 2026-11-26 the service answered 16384x16384 with `total pixel count (268435456) exceeds the maximum of 4194304`. |
| 400 parameter error | Send `auto_aspect_ratio`/`web_grounding` only to MAI-Image-2.6 and `background` only in MAI-Image-2.7 generations; the helper enforces both before any request. The service validates sizes first, so invalid-size probes cannot show whether it rejects these fields. |
| Content-safety rejection | Explain the refusal. Do not evade filters or disable safety settings. |
| 429 | Serialize requests; a deployment allows its capacity in requests per minute (`requests_per_minute` in `config.json`), shared by every caller. Wait for Retry-After. The helper retries only 429, at most twice if explicitly configured. |
| Timeout, disconnection, or 5xx | Outcome/billing may be unknown. Inspect the error before deliberately requesting another image. |
| Existing output filename | Choose a new name; no overwrite flag is provided. |
| Model unavailable after its catalog deprecation date (MAI-Image-2.7: 2027-02-17; MAI-Image-2.6: 2027-05-01) | Recheck the live catalog. Ask before upgrading, substituting, or redeploying. |

## Official references

- [MAI image API and deployment](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-mai-image)
- [Account ARM schema](https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/accounts)
- [Azure CLI REST authentication](https://learn.microsoft.com/cli/azure/use-azure-cli-rest-command)
- [Get an Azure CLI access token](https://learn.microsoft.com/cli/azure/account#az-account-get-access-token)
- [Keyless inference and RBAC](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/configure-entra-id)
