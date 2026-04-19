# Voice Air-Gap Setup: Whisper.cpp + Piper

This guide covers installing Whisper.cpp (STT) and Piper TTS for air-gap
deployments. Both providers run fully offline — no network calls, no cloud
API keys. Suitable for federal, DOE, and SCIF environments.

---

## Whisper.cpp (Speech-to-Text)

### What it is

[Whisper.cpp](https://github.com/ggerganov/whisper.cpp) is a C/C++ port of
OpenAI's Whisper model. It runs on CPU only (also GPU-accelerated when
built with CUDA or Metal). It produces no network traffic and keeps all
audio data on the local machine.

### Install: macOS (Homebrew)

```bash
brew install whisper-cpp
```

Verify:

```bash
whisper-cpp --help
```

### Install: Linux (build from source)

```bash
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
make -j$(nproc)
# Optionally install to PATH:
sudo cp main /usr/local/bin/whisper-cpp
```

For GPU acceleration on CUDA systems:

```bash
make WHISPER_CUDA=1 -j$(nproc)
```

### Download a GGML model

After installing the binary, download a model file. The default expected
location is `~/.cache/whisper-cpp/models/ggml-base.en.bin`.

**Using the bundled download script (source build):**

```bash
cd whisper.cpp
bash models/download-ggml-model.sh base.en
mkdir -p ~/.cache/whisper-cpp/models
cp models/ggml-base.en.bin ~/.cache/whisper-cpp/models/
```

**Manual download (Homebrew or binary-only install):**

```bash
mkdir -p ~/.cache/whisper-cpp/models
curl -L \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin" \
  -o ~/.cache/whisper-cpp/models/ggml-base.en.bin
```

Available model sizes (accuracy vs. speed trade-off):

| Model     | Size  | VRAM  | Notes                          |
|-----------|-------|-------|--------------------------------|
| tiny.en   | 75 MB | 1 GB  | Fastest, English only          |
| base.en   | 142 MB| 1 GB  | Good balance, English only     |
| small.en  | 466 MB| 2 GB  | Better accuracy, English only  |
| medium.en | 1.5 GB| 5 GB  | High accuracy, English only    |
| large-v3  | 2.9 GB| 10 GB | Best accuracy, multilingual    |

### ArcAgent configuration

```toml
[modules.voice]
stt_provider = "whisper_cpp"
whisper_cpp_binary = "whisper-cpp"   # or absolute path if not on PATH
# whisper_cpp_model is used as a path override when it contains '/'.
# Leave blank to use the default cache at ~/.cache/whisper-cpp/models/ggml-base.en.bin
whisper_cpp_model = ""
whisper_cpp_threads = 4
transcribe_timeout_s = 120
```

To specify a custom model path explicitly:

```toml
whisper_cpp_model = "/opt/models/ggml-medium.en.bin"
```

### Verify installation

```bash
# Generate a test WAV (1 second of silence)
python3 -c "
import wave, struct
with wave.open('/tmp/test.wav', 'w') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(struct.pack('<16000h', *([0]*16000)))
"
whisper-cpp -m ~/.cache/whisper-cpp/models/ggml-base.en.bin -f /tmp/test.wav
```

---

## Piper TTS (Text-to-Speech)

### What it is

[Piper](https://github.com/rhasspy/piper) is a fast local neural TTS system
built by the Rhasspy team. It uses ONNX voice models and runs fully offline.

### Install: pip (Python environment)

```bash
pip install piper-tts
```

This installs the `piper` command-line entry point into the active
virtualenv. Verify:

```bash
piper --help
```

### Install: pre-built binary (no Python required)

Download from [GitHub Releases](https://github.com/rhasspy/piper/releases):

```bash
# Example for Linux x86-64
curl -L "https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz" \
  | tar -xz
sudo mv piper /usr/local/bin/
```

For macOS:

```bash
curl -L "https://github.com/rhasspy/piper/releases/latest/download/piper_macos_x64.tar.gz" \
  | tar -xz
sudo mv piper /usr/local/bin/
```

### Download a voice model

Piper voice models are ONNX files paired with a `.onnx.json` config file.
The default expected location is
`~/.cache/piper/voices/en_US-libritts-high.onnx`.

**Browse available voices:**
[https://huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)

**Download the default voice:**

```bash
mkdir -p ~/.cache/piper/voices

# Download ONNX model + config
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
curl -L "${HF_BASE}/en/en_US/libritts/high/en_US-libritts-high.onnx" \
  -o ~/.cache/piper/voices/en_US-libritts-high.onnx
curl -L "${HF_BASE}/en/en_US/libritts/high/en_US-libritts-high.onnx.json" \
  -o ~/.cache/piper/voices/en_US-libritts-high.onnx.json
```

Other commonly used voices:

| Voice ID              | Language | Quality | Size   |
|-----------------------|----------|---------|--------|
| en_US-libritts-high   | en-US    | high    | ~120 MB|
| en_US-lessac-medium   | en-US    | medium  | ~60 MB |
| en_GB-alan-medium     | en-GB    | medium  | ~60 MB |
| de_DE-thorsten-medium | de-DE    | medium  | ~60 MB |
| fr_FR-siwis-medium    | fr-FR    | medium  | ~60 MB |

Download any voice by replacing `libritts/high/en_US-libritts-high` with
the appropriate path from the HuggingFace repository.

### ArcAgent configuration

```toml
[modules.voice]
tts_provider = "piper"
piper_binary = "piper"        # or absolute path if not on PATH
# piper_model is used as a path override when it contains '/'.
# Leave blank to use the default cache at ~/.cache/piper/voices/en_US-libritts-high.onnx
piper_model = ""
piper_data_dir = ""           # leave blank; piper auto-detects from model location
synthesize_timeout_s = 30
```

To specify a custom voice path:

```toml
piper_model = "/opt/voices/en_US-lessac-medium.onnx"
```

### Verify installation

```bash
echo "Arc is ready." | piper \
  --model ~/.cache/piper/voices/en_US-libritts-high.onnx \
  --output_file /tmp/test_tts.wav
# Play the audio
afplay /tmp/test_tts.wav   # macOS
aplay /tmp/test_tts.wav    # Linux
```

---

## Federal Deployment Checklist

Before deploying to a federal environment (DOE, SCIF, FedRAMP), verify:

- [ ] Both binaries are installed on PATH (or configured with absolute paths).
- [ ] Model files are in the expected cache locations (no network access
      assumed at runtime).
- [ ] ArcAgent `tier = "federal"` forces `air_gap = true` — cloud providers
      (ElevenLabs, OpenAI Whisper API) are refused at module startup.
- [ ] Binaries and model files are checksummed and recorded in the deployment
      inventory for NIST 800-53 CM-3 (Configuration Change Control).
- [ ] All audio data stays on the local machine — Whisper.cpp and Piper make
      zero outbound connections.
- [ ] PII redaction is confirmed active: `redact_pii = true` (enforced
      automatically at federal tier by ArcAgent; verify in audit logs).

### Test both providers are reachable

```python
from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider
from arcagent.modules.voice.providers.piper import PiperProvider

wc = WhisperCppProvider()
p = PiperProvider()

print("whisper-cpp available:", wc._available)
print("piper available:", p._available)
```

Both must print `True` before the agent processes voice input.

---

## Troubleshooting

### `whisper-cpp binary not found on PATH`

The binary is not installed or not on PATH. Run `which whisper-cpp` to
confirm.  On macOS with Homebrew: `brew install whisper-cpp`. On Linux:
build from source and copy `main` to a directory in PATH.

### `Whisper.cpp model not found. Searched: ~/.cache/whisper-cpp/...`

No GGML model file exists at the expected location. Download a model
using the instructions above.

### `piper binary not found on PATH`

Install via `pip install piper-tts` or download the pre-built binary from
GitHub Releases.

### `Piper voice model not found. Searched: ~/.cache/piper/voices/...`

No ONNX voice model exists at the expected location. Download a voice
model from the Piper HuggingFace repository using the instructions above.
Remember to download both the `.onnx` file and the `.onnx.json` config
file — Piper needs both.

### Whisper.cpp produces garbled output

The audio file must be 16kHz mono WAV. Pre-process with ffmpeg:

```bash
ffmpeg -i input.mp3 -ar 16000 -ac 1 output.wav
```

### Piper output is silent or empty

Check that the `.onnx.json` config file is present alongside the `.onnx`
model file. Piper requires both.
