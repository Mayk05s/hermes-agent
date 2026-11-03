# Antigravity image fallback

`openai-codex` remains the primary image backend. Set the following in the
active profile's `config.yaml` to retry failed GPT image requests through the
registered Antigravity backend:

```yaml
image_gen:
  provider: openai-codex
  model: gpt-image-2-medium
  fallback_providers: [antigravity]
  antigravity:
    command: /home/hermes/.local/bin/agy-image
    model: gemini-3.8-flash-low
    timeout: 240
```

The helper `runner.py` is installed at
`~/.local/libexec/antigravity/agy_image.py`, with an `agy-image` executable
that invokes it using a Python environment containing `requirements.txt`.
The runtime directory must contain the authenticated local bridge module,
`agy-native`, and its CPU compatibility executable. It defaults to
`~/.local/libexec/antigravity`; `ANTIGRAVITY_RUNTIME_DIR` can override it.

The helper uses a fresh temporary HOME, copies only OAuth/installation state
and the exact supplied references, and waits for a real JPEG. It does not copy
previous conversations or expose Hermes credentials to the CLI. Local paths,
HTTP(S), and base64 data references are validated and materialized in order.
Reference files are never silently discarded. Output is validated and saved
under the active profile's `cache/images`, with a unique name.

The fallback receives the original prompt, references, and aspect ratio;
its model is chosen independently from the GPT image tier. Successful tool
results identify `provider: antigravity`, `fallback_used: true`,
`fallback_from: openai-codex`, and the primary error. Invalid arguments and
unreadable references are returned directly. If both providers fail, the
primary error is retained along with `fallback_attempts`. The image tool
remains available when GPT credentials are missing but the fallback is ready.

The native CLI executes its image/file tools noninteractively; this permission
mode applies to this dedicated helper only. It does not change the permission
mode of the Hermes text fallback service. Output is JPEG; alpha output is not
supported by this helper.
