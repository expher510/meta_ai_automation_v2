# Meta AI Automation (Text / Image / Video)

Playwright automation for [meta.ai](https://meta.ai) running on GitHub Actions.

The bot accepts a prompt + cookies, then sends result to your webhook as one of:
- `text`
- `image`
- `video`
- `none`
- `error`

> Important: `auto` mode is currently unstable/not reliable.  
> Use explicit mode only: `text`, `image`, or `video`.

## What Changed

- OOP refactor in `meta_ai_bot.py`:
  - isolated handlers per mode (`text`, `image`, `video`, `auto`)
  - isolated cookie parsing / response extraction / webhook sending
- Added `--mode` argument
- Added support for cookies input as:
  - JSON list
  - JSON object with `{"cookies":[...]}`
  - Netscape cookie text
  - Base64 of any of the above
- Updated GitHub Actions workflow to pass `mode`

## Project Files

- `meta_ai_bot.py` - automation bot
- `.github/workflows/generate_video.yml` - GitHub Actions workflow
- `requirements.txt` - dependencies

## Local Run

```bash
pip install -r requirements.txt
playwright install chromium
```

### Run text mode

```bash
python meta_ai_bot.py \
  --mode text \
  --cookies "cookies.local.json" \
  --prompt "what is 2+2? answer in one short sentence" \
  --webhook "https://your-webhook"
```

### Run image mode

```bash
python meta_ai_bot.py \
  --mode image \
  --cookies "cookies.local.json" \
  --prompt "generate image about [a photorealistic orange cat astronaut on the moon]" \
  --webhook "https://your-webhook"
```

### Run video mode

```bash
python meta_ai_bot.py \
  --mode video \
  --cookies "cookies.local.json" \
  --prompt "generate video about [a drone shot flying over snowy mountains at sunrise]" \
  --webhook "https://your-webhook"
```

### Run auto mode (detect first available output)

```bash
python meta_ai_bot.py \
  --mode auto \
  --cookies "cookies.local.json" \
  --prompt "your prompt" \
  --webhook "https://your-webhook"
```

Warning: `auto` is not reliable right now. Keep it for testing only.

## GitHub Actions

Workflow file: `.github/workflows/generate_video.yml`

### Trigger with `workflow_dispatch`

Inputs:
- `prompt` (required)
- `webhook_url` (required)
- `cookies_b64` (required)
- `mode` (required, default `auto`)
- `job_id` (optional)

Recommendation: send an explicit `mode` (`text` / `image` / `video`) and avoid `auto` for production.

### Trigger with `repository_dispatch`

Use event type:
- `run_meta_ai`

Expected `client_payload`:

```json
{
  "prompt": "generate image about [a cyberpunk street at night]",
  "webhook_url": "https://your-webhook",
  "cookies_b64": "BASE64_COOKIES_STRING",
  "mode": "image",
  "job_id": "optional-job-id"
}
```

If `mode` is missing, workflow defaults to `auto`.

Warning: defaulting to `auto` may return inconsistent behavior. Prefer sending `mode` explicitly.

## Webhook Payload

The bot always sends one payload with this shape:

```json
{
  "job_id": "optional-job-id",
  "success": true,
  "prompt": "original prompt",
  "mode_requested": "video",
  "output_type": "video",
  "video_urls": ["..."],
  "video_count": 4,
  "image_urls": [],
  "image_count": 0,
  "text_response": null,
  "error": null
}
```

Notes:
- `mode_requested` is what you asked for (`text`/`image`/`video`/`auto`)
- `output_type` is what Meta actually returned

## n8n Notes

- Save cookies in Redis as Base64 string (`meta_cookies_b64`)
- At trigger time, read that key and pass it as `cookies_b64`
- Store callback body directly by `job_id` for polling

## Troubleshooting

- `No ... found`:
  - cookies expired, refresh cookies
  - prompt may not match requested mode
- Webhook not receiving:
  - verify URL is public and reachable
- Action not triggered:
  - check token `repo` scope and event type `run_meta_ai`
