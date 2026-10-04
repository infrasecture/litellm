# Infrasecture LiteLLM

This fork preserves ChatGPT Responses caller instructions verbatim, including empty and absent values. The adapter no longer prepends its bundled Codex prompt or the legacy `CHATGPT_DEFAULT_INSTRUCTIONS` environment override

It also preserves `prompt_cache_key` in Responses requests and uses a nonempty string key as the upstream `session_id` header. Empty, absent or non-string keys leave the existing session header unchanged

It also carries the standalone Codex search implementation from [upstream PR #36180](https://github.com/BerriAI/litellm/pull/36180), adapted to the current provider, proxy and test layout. Both `/alpha/search` and `/v1/alpha/search` use the shared passthrough lifecycle, ChatGPT OAuth, model routing, limits and callbacks

`main` carries the patches on upstream HEAD. `infrasecture_v1.105.0-rc.1` carries the same patches on upstream tag `v1.105.0-rc.1`. The upstream tag is not moved or replaced

GitHub Actions builds each branch from source using its locked dependencies and Dockerfile, tests the packaged proxy against a local synthetic provider with external networking disabled, then publishes native amd64 and arm64 images to `ghcr.io/infrasecture/litellm`

The moving image tags are `main` and `v1.105.0-rc.1`. Every publication also has a `sha-<full Git commit>` tag and a manifest digest. Deployments should use the digest. A channel tag is advanced only after both architectures pass the packaged proxy checks

The fork does not include the separate Fungophilus changes for Responses JSON schema forwarding. Those remain separate patches until adopted upstream or explicitly added here
