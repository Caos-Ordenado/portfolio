# llama — local LLM gateway (llama-swap + llama.cpp)

Replaces the old Ollama deployment. It runs one `llama-server` process at a time and swaps models on demand. MoE experts are offloaded to system RAM (`--n-cpu-moe`), so 12GB of VRAM can run 20–35B models.

Everything runs on caos. The API is only *OpenAI-compatible* in format; no external provider is involved.

## Hardware (caos)

| Part | Value | Why it matters |
|---|---|---|
| GPU | RTX 4070 Ti 12GB, PCIe 4.0 x16 | attention + KV cache + some experts |
| CPU | i9-13900K, 8 P-cores (CPU 0-15 with HT) + 16 E-cores (CPU 16-31) | 16 threads pinned to P-cores: `--threads 16 -Cr 0-15` (+6% vs unpinned 8 threads) |
| RAM | 2×16GB DDR5-5600 dual channel (~89GB/s) | bandwidth caps tok/s for CPU-resident experts |
| Driver | NVIDIA **580+** required | image is built on CUDA 12.8.1 (driver 550 is too old) |

Host setup and known hardware issues (driver, CPU frequency cap, pinned-memory leak): see `scripts/caos-host.md` in the workspace root.

## Endpoints

| From | URL |
|---|---|
| In-cluster | `http://llama.default.svc.cluster.local:8080` |
| LAN / Tailscale | `http://home.server:30080/llm` |

- `GET /health`: gateway health
- `GET /v1/models`: models and aliases
- `POST /v1/chat/completions`: OpenAI chat API (text, `image_url` parts, `response_format`)
- `GET /running`: currently loaded model. `GET /unload` unloads all models.
- `/ui`: llama-swap web UI (logs, models)

The route has no auth, the same exposure as the old `/ollama` route (LAN + Tailscale only).

## Models

| Alias | Model | Use | Context |
|---|---|---|---|
| `coder` | Qwen3.6-35B-A3B UD-Q4_K_M (22GB) | OpenCode, agents, tool calling | 128K, KV q8_0 |
| `reasoning` | gpt-oss-20b MXFP4 (12GB) | reasoning / docs | 64K |
| `extract` | Qwen3.5-9B Q4_K_M (6GB), fully on GPU, thinking off | JSON extraction (scraping pipeline) | 64K / 2 slots |
| `vision` | Gemma 4 26B-A4B QAT UD-Q4_K_XL + mmproj (15GB) | screenshots → JSON | 32K / 2 slots |

- Only one model is resident at a time. A request for another model waits while llama-swap swaps them (typically 10–60s), which is why `LLMClient` defaults to a 180s timeout.
- Idle models unload after 30 min (`globalTTL`).

Code should use the constants in `shared.llm_client` (`MODEL_CODER`, `MODEL_VISION`, `MODEL_REASONING`, `MODEL_EXTRACT`), never GGUF names.

## Measured performance (2026-09-27, driver 580.178, llama.cpp b11206)

| Alias | Generation | Prompt processing | VRAM | Notes |
|---|---|---|---|---|
| `coder` | 51 tok/s (50 at 12K ctx) | ~2000 tok/s (12K prompt) | 11.3GB | tool calling OK, OpenCode edit task OK |
| `reasoning` | 60 tok/s | — | 11.0GB | |
| `vision` | 51 tok/s | ~340 tok/s | 11.1GB | full-page crawler screenshot → JSON in ~21-26s end to end |
| `extract` | 78 tok/s | ~300 tok/s | 7.3GB | fully on GPU |

A model swap takes ~10-20s when the GGUF is already in page cache.

The other home-lab services on caos use ~2.5GB RAM at rest. The pod requests 8Gi (limit 24Gi), and its anonymous RSS is 1.5-6GB; weights are mmap'd page cache the kernel can reclaim.

## Deploy

```bash
# 0. One-time migration: free the GPU from the old Ollama pod. `apply -k` does not prune it.
#    ollama-pvc is kept on purpose, for rollback.
kubectl delete deployment/ollama service/ollama ingressroute/ollama middleware/ollama-stripprefix --ignore-not-found

# 1. PVC + gateway
kubectl apply -k portfolio/k8s/llama/

# 2. Download the GGUF catalog (~56GB, one-shot, idempotent)
kubectl delete job llama-model-fetch --ignore-not-found
kubectl apply -f portfolio/k8s/llama/model-fetch-job.yaml
kubectl logs -f job/llama-model-fetch

# 3. Check
kubectl rollout status deployment/llama
curl http://home.server:30080/llm/health
curl http://home.server:30080/llm/v1/models
```

After editing `configmap.yaml`, bump the `llama/config-revision` annotation in `deployment.yaml` and re-apply. The config is mounted with `subPath`, so it does not hot-reload.

## Tuning `--n-cpu-moe`

`--n-cpu-moe N` keeps the experts of the first N layers on CPU:
- Lower N puts more on the GPU, which is faster until VRAM overflows.
- Tuned values: coder 28, vision 18, reasoning 8. Coder at 26 or lower fails to load at 128K. Vision needs ~1.5GB spare VRAM for the image encoder on full-page screenshots, and `--image-max-tokens 1120` caps that.

1. Watch VRAM: `ssh caos@home.server 'watch -n1 nvidia-smi'`.
2. Send a long request to the alias with the target context filled, and read the `timings` in the response (`predicted_per_second`):
   ```bash
   curl -s http://home.server:30080/llm/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"coder","messages":[{"role":"user","content":"Write a 600-word essay about Kubernetes."}],"max_tokens":800}' \
     | jq '.timings'
   ```
3. Lower N by 2 and repeat until VRAM gets to ~11.5GB used or you see an OOM or a slowdown. Then go back up one step. Leave headroom for the KV cache at full context.
4. Targets: coder ≥ 30 tok/s, vision ≥ 35 tok/s, reasoning ≥ 30 tok/s.

## Adding a model

1. Add a `hf download` line to `model-fetch-job.yaml` and re-run the Job.
2. Add a `models:` entry in `configmap.yaml` (reuse the `${server}` macro) with an alias.
3. Bump `llama/config-revision` and apply.
4. If code should use it, add a constant to `portfolio/shared/shared/shared/llm_client.py`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Pod `Pending` | another pod holds `nvidia.com/gpu`: `kubectl describe pod -l app=llama` |
| `CUDA driver version is insufficient` | host driver < 570. Upgrade: `sudo apt install nvidia-driver-580-server`, then reboot |
| `OOMKilled` | pod limit (24Gi) hit. Check that `GGML_CUDA_NO_PINNED=1` is set and `-lm none` / `--no-mmap` are NOT used: both duplicate the CPU experts in anonymous memory |
| `cudaMalloc failed` at load | N too low for the context. Raise `--n-cpu-moe` |
| `CUDA error: out of memory` in `clip_image_batch_encode` | vision image too large for free VRAM. Raise vision `--n-cpu-moe` or lower `--image-max-tokens` |
| Empty `content`, `finish_reason: length` | model spent the budget thinking. Raise `max_tokens`, or add `--reasoning-budget 0` for extraction models |
| Node slow / swapping, `free` shows RAM used but no process owns it | orphaned NVIDIA pinned memory from killed `llama-server` processes. Compare `Active(anon)+Inactive(anon)` against `AnonPages` in `/proc/meminfo`; reboot caos to reclaim it. `GGML_CUDA_NO_PINNED=1` prevents it |
| Requests time out during swaps | raise `LLM_TIMEOUT_S` in the client env |

Logs: `kubectl logs -l app=llama -f` (proxy + upstream llama-server output).

## Rollback

`git revert` the migration commit(s) and `kubectl apply -k portfolio/k8s/`. The old `ollama-pvc` is kept until the new stack has been verified. Delete it afterwards with `kubectl delete pvc ollama-pvc`, which frees ~50GB.

## Future hardware

- **64GB RAM**: replace the kit with 2×32GB DDR5-5600. Do not add 2 more DIMMs; four DIMMs drop to ~4800 MT/s. This unlocks GLM-4.5-Air-class models (~106B MoE) and Qwen3.6 at Q6/Q8.
- **Second GPU**: llama.cpp splits across GPUs with `--tensor-split` / `-dev`. Request `nvidia.com/gpu: 2` and re-tune `--n-cpu-moe` (fewer experts on CPU).
