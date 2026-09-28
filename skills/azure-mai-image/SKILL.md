---
name: azure-mai-image
description: Generate and iteratively edit original images with MAI-Image-2.6 in Azure Foundry using Azure CLI sign-in and short-lived Entra ID tokens. Use when the user asks to create, draw, render, revise, or improve an image, diagram, illustration, concept art, product visual, background, or MAI image. Generates at the largest size the service allows for any aspect ratio (up to 2.36 MP, e.g. 2048x1152), edits with up to five reference images, and can add a clearly labelled 4K or 8K upscale for full-bleed slides. Saves PNG files locally; no API keys or Python packages required.
---

# Azure MAI Image

Generate or revise one image per invocation with `scripts/generate.py`. Before
first use, configure `config.json` for an Entra-authenticated Azure Foundry
deployment. Do not provision resources or change permissions during ordinary
image generation.

## Quick start

```bash
python3 "$HOME/.copilot/skills/azure-mai-image/scripts/generate.py" \
  --prompt "An original watercolor of a quiet alpine lake at sunrise" \
  --output "$PWD/alpine-lake.png"
```

The generator requires Azure CLI and Python 3.10+. It acquires a token with
`az account get-access-token`, keeps it in memory, calls the MAI image API, and
saves a PNG. It returns compact JSON with the absolute image path, dimensions,
size, and checksum, not the token or base64 image. Use the returned path to show
or attach the image to the user.

Resolve `scripts/`, `config.json`, and `references/` relative to this skill's
directory if installed elsewhere.

### Maximum resolution for slides and screens

```bash
python3 "$HOME/.copilot/skills/azure-mai-image/scripts/generate.py" \
  --prompt-file "$PWD/title.txt" --aspect 16:9 --upscale 4k \
  --output "$PWD/title.png"
```

`--aspect 16:9` requests 2048x1152, the largest 16:9 image the service returns.
`--upscale 4k` additionally writes `title-4k.png` (3840x2160) by interpolation. The native file is kept, and the
summary JSON describes both files. See "Resolution" below.

## Agent workflow

1. Turn the user's image request into a clear prompt: subject, composition,
   lighting, style, palette, and any exact requested text. Do not invent extra
   requirements or substitute another model.
2. Choose a new `.png` output path in the user's working/artifact directory.
   The parent directory must exist. Existing files are never overwritten.
3. Run the helper once. For long prompts, use `--prompt-file` with a UTF-8 file.
4. Read the returned image with an image-capable tool and provide the file to
   the user. Clearly identify generated content as AI-generated when publishing.
5. For a revision, pass the previous PNG with `--input-image` and describe only
   the changes and what must remain. To combine sources (for example a base image
   plus a logo, product shot or icon sheet), repeat `--input-image` up to five
   times: the first file is the image being edited, and the prompt refers to the
   others by position ("the second image"). Revisions are additional billable requests.
   Generate only requested assets; do not start unbounded variation/retry loops.

## Revise an existing image

```bash
python3 "$HOME/.copilot/skills/azure-mai-image/scripts/generate.py" \
  --input-image "$PWD/diagram-v1.png" \
  --prompt "Keep the composition, colors, and all other labels. Enlarge the text, remove the decorative arrow, and connect Browser to Flask with one right-pointing arrow." \
  --output "$PWD/diagram-v2.png"
```

### Combine several reference images

```bash
python3 "$HOME/.copilot/skills/azure-mai-image/scripts/generate.py" \
  --input-image "$PWD/scene.png" --input-image "$PWD/logo-sheet.png" \
  --prompt "Keep the first image unchanged except the empty shelf: place the three objects from the second image on it, in their original colors." \
  --output "$PWD/scene-v2.png"
```

References must be PNG or JPEG. The helper sends `model`, `prompt`, optional
boolean flags, and one binary `image` part per reference, repeated in the order
given, as multipart form data to the documented MAI edits route (up to five
references). It does not send local filenames/paths. A 20 MiB per-file local upload
guardrail applies; this is not a stated service limit. With several references,
`input_sha256` in the JSON summary is a list in the same order.

Do not pass `--width` or `--height` for edits: the documented API accepts those
only for generation. Ask the model to preserve composition/aspect in the prompt,
or opt into `--auto-aspect-ratio`. Inspect the returned dimensions and composition;
reference editing is generative, not a pixel-exact patch or a layout guarantee.
Every revision saves a new file, preserving the previous versions.

### Visual revision loop

When the user requests iteration:

1. Generate a first version and actually view it.
2. Check it against a concrete list: factual labels, arrow direction/order,
   contrast, text size at publication width, composition, and unwanted elements.
3. Name specific defects, then edit that PNG with a focused correction prompt.
   Preserve good parts explicitly. View and compare the revision.
4. Keep the best factual/readable version, not automatically the newest.
   Stop once the requested result meets the checks. If two edits fail to improve
   the same issue, change the layout/prompt approach or report the remaining
   issue instead of repeating an ineffective prompt.

Serialize all calls and keep at least 60 seconds between starts. Do not launch
automatic visual-quality loops, parallel requests, or billable background
variations the user did not request. Label published artwork as AI-generated.
For instructional diagrams, compare every label and arrow against the actual
code; visual polish must not introduce fictional APIs or capabilities.

## Resolution: what the service allows

The limit is the service's, not the helper's. The **maximum total pixel count per image is 2,359,296 on
MAI-Image-2.6 and MAI-Image-2.6-Flash** (equivalent to 1536x1536) and 1,048,576 on MAI-Image-2.5,
2.5-Flash and 2.5-Pro. Each side must be at least 768 px. The output is always PNG.

Native 4K is therefore not available. A live request for 3840x2160 on 2026-09-28 was rejected:
`HTTP 400 [unsupported_request_value] ... total pixel count (8294400) exceeds the maximum of 2359296`.

Largest sizes that `--aspect` selects (multiples of 16, aspect within 1%):

| Aspect | MAI-Image-2.6 / 2.6-Flash | MAI-Image-2.5 family |
| --- | --- | --- |
| 16:9 / 9:16 | 2048x1152 / 1152x2048 | 1360x768 / 768x1360 |
| 1:1 | 1536x1536 | 1024x1024 |
| 4:3 / 3:4 | 1776x1328 / 1328x1776 | 1168x880 / 880x1168 |
| 3:2 / 2:3 | 1872x1248 / 1248x1872 | 1248x832 / 832x1248 |
| 2:1 | 2144x1072 | not possible (sides >=768) |
| 21:9 | 2320x992 | not possible |
| 4:1 | 3072x768 | not possible |

- **Edits** have no size parameter. They return about 1 MP at the first reference's aspect ratio. Measured:
  a 16:9 reference returns 1360x768, a square one 1024x1024. The helper keeps such results even when a side
  is below 768 px, because the request was already billed. For a larger composition, generate with `--aspect`
  and describe the layout in the prompt.
- `--auto-aspect-ratio` lets MAI-Image-2.6 choose the framing, so the returned size may differ.
- **Upscaling** (`--upscale 4k|8k|2x|WxH`, or `--upscale-from FILE` for an existing image, with no API call)
  interpolates. It uses Pillow Lanczos resampling with a light unsharp mask when Pillow is installed, and
  macOS `sips` otherwise. It gives headroom for 4K displays and full-bleed slides but adds no detail:
  label it as upscaled, and keep the native file as the source of truth.

## Options and limits

| Option | Behavior |
| --- | --- |
| `--prompt TEXT` / `--prompt-file FILE` | Exactly one input prompt. |
| `--input-image FILE` | Edit with a PNG/JPEG reference; repeat for up to five references (the first is the base image). Requires a prompt. Cannot combine with `--response-json`. |
| `--output FILE.png` | Required; safe no-overwrite output. |
| `--width N --height N` | Generation only. Default 1024x1024. Each dimension >=768; product <=2,359,296 on MAI-Image-2.6 models (for example 2048x1152 for full-bleed 16:9 slides), <=1,048,576 on MAI-Image-2.5 models. |
| `--aspect W:H` | Generation only; instead of `--width/--height`. Picks the largest valid size for the ratio (table above). |
| `--upscale SPEC` | Also write an interpolated copy: `4k` (long side 3840), `8k` (7680), a factor such as `2x`, or an exact `WxH` with the same aspect. Saved as `<output>-<spec>.png`; the spec is checked before the billable request. |
| `--upscale-from FILE` | Upscale an existing PNG/JPEG to `--output` (needs `--upscale`); no Azure call, no token. |
| `--deployment NAME` | Use another deployment listed in `config.json` `deployments` (for example a future MAI-Image-2.6-Flash deployment). Its model sets the size limit and whether the 2.6-only flags are allowed. |
| `--auto-aspect-ratio` | MAI-Image-2.6 models only: let the model choose framing; returned dimensions may differ. |
| `--web-grounding` | MAI-Image-2.6 models only: Bing-grounded generation; off unless the user requests it. |
| `--timeout N` | Network I/O timeout, default 300 seconds; allowed 1-600. |
| `--retries 0\|1\|2` | HTTP 429 retries only; default 1, with bounded Retry-After waits. |
| `--response-json FILE` | Decode a saved `az rest` response without another API call. |

The documented request parameters are `model`, `prompt` (up to 32,000 tokens), `image` (edits; up to five
JPEG/PNG files), `width` and `height` (generations only), and `auto_aspect_ratio` and `web_grounding`
(MAI-Image-2.6 models only). The helper sends the last two only to 2.6 deployments. Do not use OpenAI `size`,
`quality`, `n`, or `response_format` parameters: this is the MAI API, not the OpenAI images API. The output is
one PNG per request for both generation and editing.

There is no mask, seed or negative-prompt parameter. To change one region, name it precisely in the prompt
and say what must stay unchanged.

Offline helper checks (no Azure calls):

```bash
python3 -m unittest discover -s "$HOME/.copilot/skills/azure-mai-image/scripts" -p 'test_*.py'
```

The deployed capacity is **1 request per 60 seconds**, shared across agents.
Serialize generation and allow at least 60 seconds between starts. Capacity
is a rate/quota allocation, not a spending cap. Global Standard is usage-billed
and can process requests outside Sweden. Do not quote a price without checking
current Azure pricing.

The documented Global Standard tiers allow 6 to 36 RPM for MAI-Image-2.6 and 2.6-Flash, and 2 to 12 RPM
for the 2.5 models. On 2026-09-28 the subscription's Sweden Central quota for MAI-Image-2.6 was 6, of which
2 were in use. Raising the `mai-image` capacity changes a shared Azure deployment: do it only with the user's
explicit approval, and never as part of ordinary generation.

## Authentication and safety

- Use the existing Azure CLI sign-in. Every call pins the configured
  subscription, without changing the CLI's global default.
- If login has expired, have the user sign in with
  `az login --tenant <tenant-id>`.
- The token resource is `https://cognitiveservices.azure.com`; its OAuth scope
  equivalent is `https://cognitiveservices.azure.com/.default`.
- Tokens are short-lived credentials, not persistent deployment API keys.
  Never print, paste, commit, export, or write tokens to files, `.env`, logs,
  skill text, or chat. Do not use `az --debug`, shell tracing, or an HTTP trace.
- The helper passes tokens only in in-memory HTTPS headers, validates the
  configured Azure endpoint, and refuses redirects. Do not bypass TLS checks.
- Public means internet-reachable, **not anonymous**. API-key authentication is
  disabled. Use existing inference RBAC; do not create keys, identities, or
  role assignments without specific user approval.
- Do not send secrets, credentials, or confidential source code in prompts.
  Do not send sensitive personal/company material without explicit user
  authorization. Web grounding remains off unless requested.
- Respect content-safety refusals; do not retry with evasive prompts or disable
  safety controls. Never silently fall back to a different model.
- Timeouts and 5xx errors are not automatically retried: the request may have
  been processed and billed. An error is not a successfully generated image.

## Deployment

Set the subscription, tenant, resource, endpoint, deployment, model, and model
version in [config.json](config.json). The resource must allow Entra-authenticated
inference, and the signed-in identity needs inference permission. Preview model
versions can expire; recheck the live Azure catalog rather than automatically
changing the configured model or deployment.

## CLI reference and troubleshooting

Read [Azure CLI and token reference](references/azure-cli.md) for direct
`az rest` usage, the explicit token command, deployment commands, and diagnosis
of 401/403/404/429 errors.

Authoritative sources, consulted 2026-09-14 (sizes, parameters and quotas rechecked 2026-09-28):

- [MAI image deployment, API, capabilities, and limits](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-mai-image)
- [Azure CLI REST authentication](https://learn.microsoft.com/cli/azure/use-azure-cli-rest-command)
- [Azure CLI access tokens](https://learn.microsoft.com/cli/azure/account#az-account-get-access-token)
- [Foundry keyless authentication and RBAC](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/configure-entra-id)

For API changes, search official Microsoft Learn documentation for
`MAI-Image-2.6 image generations` and fetch the relevant page. MAI-specific
authentication documentation takes precedence over other Foundry API audiences.
