---
name: azure-mai-image
description: Generate and iteratively edit original images with MAI-Image-2.7 in Azure Foundry using Azure CLI sign-in and short-lived Entra ID tokens. Use when the user asks to create, draw, render, revise, or improve an image, diagram, illustration, concept art, product visual, background, transparent-background logo, icon or cut-out asset, or MAI image. Generates at the largest size the service allows for any aspect ratio (up to 4.19 MP, e.g. 2048x2048 or 2704x1520), can return transparent PNGs, edits with up to five reference images, and can add a clearly labelled 4K or 8K upscale for full-bleed slides. Saves PNG files locally; no API keys or Python packages required.
---

# Azure MAI Image

Generate or revise one image per invocation with `scripts/generate.py`. Before
first use, configure `config.json` for an Entra-authenticated Azure Foundry
deployment of MAI-Image-2.7 (the default), optionally with a MAI-Image-2.6
alternate. Do not provision resources or change permissions during ordinary
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

`--aspect 16:9` requests 2704x1520, the largest 16:9 image MAI-Image-2.7 returns (aspect within 0.1%).
`--upscale 4k` additionally writes `title-4k.png` (3840x2159) by interpolation; use `--upscale 3840x2160` for an
exact UHD frame. The native file is kept, and the summary JSON describes both files. See "Resolution" below.

### Transparent background for logos, icons and cut-outs

```bash
python3 "$HOME/.copilot/skills/azure-mai-image/scripts/generate.py" \
  --prompt "A cheerful orange toy robot, full body, centered, no shadow" \
  --transparent-background --aspect 1:1 \
  --output "$PWD/robot.png"
```

`--transparent-background` adds `"background": "transparent"` to the generation request, as in Microsoft
Learn's MAI-Image-2.7 example, for assets placed over slides, pages or other images. It works only for
generations on MAI-Image-2.7 deployments; the helper rejects it for edits, `--response-json`, `--upscale-from`
and other models before any request. Describe an isolated subject; do not ask for a checkerboard or a white
background. The summary adds `"background": "transparent"` and `"alpha"`, which says whether the returned PNG
really has transparency (on 2026-11-26 all 16 live MAI-Image-2.7 generations with this option returned alpha,
at sizes up to 4096x1024). If `alpha` is `false`, tell the user instead of retrying. Upscaling with Pillow keeps
the alpha channel.

## Agent workflow

1. Turn the user's image request into a clear prompt: subject, composition,
   lighting, style, palette, and any exact requested text. Do not invent extra
   requirements or substitute another model. For a logo, icon, sticker or
   product cut-out that must sit on other content, add `--transparent-background`.
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

References must be PNG or JPEG. The helper sends `model`, `prompt`, the boolean
2.6-only flags (on 2.6 deployments only), and one binary `image` part per reference, repeated in the order
given, as multipart form data to the documented MAI edits route (up to five
references). It does not send local filenames/paths. A 20 MiB per-file local upload
guardrail applies; this is not a stated service limit. With several references,
`input_sha256` in the JSON summary is a list in the same order.

Do not pass `--width` or `--height` for edits: the documented API accepts those
only for generation. Ask the model to preserve composition/aspect in the prompt,
or, on the MAI-Image-2.6 deployment (`--deployment mai-image`), opt into
`--auto-aspect-ratio`. Inspect the returned dimensions and composition;
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

Serialize all calls: one request at a time. A deployment's rate limit
(`requests_per_minute` in `config.json`) is shared by every agent and user of
that deployment. Do not launch
automatic visual-quality loops, parallel requests, or billable background
variations the user did not request. Label published artwork as AI-generated.
For instructional diagrams, compare every label and arrow against the actual
code; visual polish must not introduce fictional APIs or capabilities.

## Resolution: what the service allows

The limit is the service's, not the helper's. The **maximum total pixel count per image is 4,194,304 on
MAI-Image-2.7** (equivalent to 2048x2048), 2,359,296 on MAI-Image-2.6 and 2.6-Flash (1536x1536) and 1,048,576
on the MAI-Image-2.5 family (1024x1024). Each side must be at least 768 px. The output is always PNG.

Microsoft Learn does not list the MAI-Image-2.7 limit yet (its own 2.7 example uses 2048x2048). On 2026-11-26
a MAI-Image-2.7 deployment reported it in response to deliberately invalid requests (HTTP 400, no image):

- 16384x16384: `HTTP 400 [unsupported_request_value] ... Invalid dimensions 16384x16384: total pixel count (268435456) exceeds the maximum of 4194304.`
- 16x16 and 7000000x16: `... 'width' must be at least 768 pixels.` and `... 'height' must be at least 768 pixels.`

MAI-Image-2.7-Flash is assumed to share the 2.7 limit (Learn gives it the same capabilities); it was not
probed. Native 4K is still not available: 3840x2160 is 8,294,400 px. On 2.6 a live
request for it was rejected with `... total pixel count (8294400) exceeds the maximum of 2359296`.

Largest sizes that `--aspect` selects (multiples of 16, aspect within 1%):

| Aspect | MAI-Image-2.7 (default) | MAI-Image-2.6 / 2.6-Flash |
| --- | --- | --- |
| 16:9 / 9:16 | 2704x1520 / 1536x2720 | 2048x1152 / 1152x2048 |
| 1:1 | 2048x2048 | 1536x1536 |
| 4:3 / 3:4 | 2352x1760 / 1760x2352 | 1776x1328 / 1328x1776 |
| 3:2 / 2:3 | 2496x1664 / 1664x2496 | 1872x1248 / 1248x1872 |
| 2:1 | 2880x1440 | 2144x1072 |
| 21:9 | 3104x1328 | 2320x992 |
| 4:1 | 4096x1024 | 3072x768 |

The 2.7 sizes are computed from the limits above; each of them was generated live on 2026-11-26.
On the MAI-Image-2.5 family the largest sizes are 1360x768 (16:9) and 1024x1024; 2:1 and wider are impossible.

- **Edits** have no size parameter. MAI-Image-2.7 keeps the first reference's aspect ratio and size, scaled
  down to a 2048 px long side. Measured on 2026-11-26 (11 edits): 1824x960 and 1416x792 references came back
  unchanged, 2160x2160 returned 2048x2048, 2496x1344 returned 2048x1103, and a small 540x540 reference
  returned 1024x1024. MAI-Image-2.6 returns about 1 MP at the first reference's aspect ratio: a 16:9
  reference returns 1360x768, a square one 1024x1024. Check the returned dimensions. The helper keeps edit results even when a side
  is below 768 px, because the request was already billed; it rejects only results above the model's pixel
  limit. For a larger composition, generate with `--aspect` and describe the layout in the prompt.
- `--auto-aspect-ratio` lets MAI-Image-2.6 choose the framing (`--deployment mai-image` only), so the returned
  size may differ.
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
| `--width N --height N` | Generation only. Default 1024x1024. Each dimension >=768; product <=4,194,304 on MAI-Image-2.7 models (for example 2048x2048, or 2704x1520 for full-bleed 16:9 slides), <=2,359,296 on MAI-Image-2.6 models (2048x1152), <=1,048,576 on MAI-Image-2.5 models. |
| `--aspect W:H` | Generation only; instead of `--width/--height`. Picks the largest valid size for the ratio (table above). |
| `--upscale SPEC` | Also write an interpolated copy: `4k` (long side 3840), `8k` (7680), a factor such as `2x`, or an exact `WxH` with the same aspect. Saved as `<output>-<spec>.png`; the spec is checked before the billable request. |
| `--upscale-from FILE` | Upscale an existing PNG/JPEG to `--output` (needs `--upscale`); no Azure call, no token. |
| `--transparent-background` | MAI-Image-2.7 models, generation only: sends `background: transparent` for a PNG with alpha (logos, icons, cut-outs). Rejected for edits, `--response-json`, `--upscale-from` and other models before any request. The summary reports `alpha`. |
| `--deployment NAME` | Use another deployment listed in `config.json` `deployments`, for example `mai-image` (MAI-Image-2.6). Its model sets the size limit and which model-specific flags are allowed. Switch only when the user asks for 2.6 or a 2.6-only flag, never silently. |
| `--auto-aspect-ratio` | MAI-Image-2.6 models only (`--deployment mai-image`): let the model choose framing; returned dimensions may differ. |
| `--web-grounding` | MAI-Image-2.6 models only (`--deployment mai-image`): Bing-grounded generation; off unless the user requests it. |
| `--timeout N` | Network I/O timeout, default 300 seconds; allowed 1-600. |
| `--retries 0\|1\|2` | HTTP 429 retries only; default 1, with bounded Retry-After waits. |
| `--response-json FILE` | Decode a saved `az rest` response without another API call. |

The documented request parameters are `model`, `prompt` (up to 32,000 tokens), `image` (edits; up to five
JPEG/PNG files), `width` and `height` (generations only), and `auto_aspect_ratio` and `web_grounding`
(MAI-Image-2.6 models only). Learn's MAI-Image-2.7 generation example also sends `background: "transparent"`,
which its parameter table does not list yet. The helper sends the 2.6-only fields only to 2.6 deployments and
`background` only for `--transparent-background` generations on 2.7. Do not use OpenAI `size`,
`quality`, `n`, or `response_format` parameters: this is the MAI API, not the OpenAI images API. The output is
one PNG per request for both generation and editing.

There is no mask, seed or negative-prompt parameter. To change one region, name it precisely in the prompt
and say what must stay unchanged.

Offline helper checks (no Azure calls):

```bash
python3 -m unittest discover -s "$HOME/.copilot/skills/azure-mai-image/scripts" -p 'test_*.py'
```

A deployment's capacity is its rate limit: the Azure catalog lists **1 request per minute per capacity unit**
for MAI-Image-2.7 and 2.6 (Global Standard, default capacity 1), so a deployment with capacity 10 allows
**10 requests per 60 seconds** (deployment `rateLimits`, verified live on 2026-11-27 for both models). Set
`capacity` and `requests_per_minute` in `config.json` (top level and per deployment) to your deployments'
values; the template assumes capacity 10, and the helper quotes `requests_per_minute` in its 429 advice.
The limit is shared by every agent and user of that deployment. Serialize generation: one request at a
time, no parallel or rapid-fire requests. Capacity is a rate/quota allocation, not a spending cap. Global
Standard is usage-billed and can process requests outside the resource's region. Do not quote a price
without checking current Azure pricing.

Capacity is drawn from a per-subscription, per-region quota for each model; check it with
`az cognitiveservices usage list` (see the CLI reference). Learn's RPM table does not list MAI-Image-2.7 yet
(it gives 6 to 36 RPM for MAI-Image-2.6 and 2.6-Flash, and 2 to 12 for the MAI-Image-2.5 models). On
2026-11-27 one subscription's Sweden Central quota was 12 units each for MAI-Image-2.7, 2.7-Flash, 2.6 and
2.6-Flash. Raising a deployment's capacity changes a shared Azure deployment: do it only with the user's
explicit approval, and never as part of ordinary generation.

## Authentication and safety

- Use the existing Azure CLI sign-in. Every call pins the configured
  subscription, without changing the CLI's global default.
- If login has expired, have the user sign in with
  `az login --tenant <tenant-id>` (the `tenant_id` in `config.json`).
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
  safety controls. Never silently fall back to a different model or deployment.
- Timeouts and 5xx errors are not automatically retried: the request may have
  been processed and billed. An error is not a successfully generated image.

## Deployment

Set the subscription, tenant, resource, endpoint, deployments, models, versions and rate limits in
[config.json](config.json). The template expects:

- Default deployment: `mai-image-27`, model `MAI-Image-2.7` (preview), version `2026-11-02`, Global Standard,
  capacity 10.
- Optional alternate deployment: `mai-image`, model `MAI-Image-2.6` (generally available), version
  `2026-07-31`, Global Standard, capacity 10. Select it with `--deployment mai-image` only when the user asks
  for MAI-Image-2.6 or a 2.6-only option.
- API: `https://<account-name>.services.ai.azure.com/mai/v1/images/generations` (edits:
  `/mai/v1/images/edits`). The request's `model` field is the deployment name.

Rename the entries to match your deployment names and capacities, and delete any deployment you did not
create. The resource must allow Entra-authenticated inference, and the signed-in identity needs inference
permission. The [CLI reference](references/azure-cli.md) has the commands to create both deployments.

MAI-Image-2.7 is a preview model. On 2026-11-27 Azure's catalog reported **2027-02-17** as the deprecation
date for MAI-Image-2.7 and 2.7-Flash (version 2026-11-02), and **2027-05-01** for MAI-Image-2.6 and 2.6-Flash,
which are now generally available. The MAI-Image-2.5 family passed its date on 2026-11-24. Recheck the live
catalog before relying on a model beyond its date; do not automatically change the model or deployment.

## CLI reference and troubleshooting

Read [Azure CLI and token reference](references/azure-cli.md) for direct
`az rest` usage, the explicit token command, deployment commands, and diagnosis
of 401/403/404/429 errors.

Authoritative sources, consulted 2026-11-27 (MAI-Image-2.7 size limits, edit sizes, transparency, rate limits
and lifecycle were also checked live against a deployment on 2026-11-26 and 2026-11-27):

- [MAI image deployment, API, capabilities, and limits](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-mai-image)
- [Azure CLI REST authentication](https://learn.microsoft.com/cli/azure/use-azure-cli-rest-command)
- [Azure CLI access tokens](https://learn.microsoft.com/cli/azure/account#az-account-get-access-token)
- [Foundry keyless authentication and RBAC](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/configure-entra-id)

For API changes, search official Microsoft Learn documentation for
`MAI-Image-2.7 image generations` and fetch the relevant page. MAI-specific
authentication documentation takes precedence over other Foundry API audiences.
