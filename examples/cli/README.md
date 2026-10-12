# huri-cli

The reference command-line client: microphone and text prompt in; the
assistant's reply as text, speech and a live gesture view out. It is a plain
use case of the client SDK (`huri_core.core.client.HuRIClient`) and lives
outside the core package on purpose — use it as a starting point for your own
client.

```sh
pip install -e examples/cli          # from the repo root, next to huri-core
huri-cli --config config/client_full.yaml
huri-cli --config config/client_text.yaml --log-level DEBUG --log-file cli.log
```

## What it handles

| Topic | Direction | What it does |
|---|---|---|
| `huri.audio.in` | sends | microphone, 16-bit mono frames |
| `huri.question` | sends | the `>>` prompt, one question per line (`\exit` quits) |
| `huri.token` | receives | prints the reply as it streams |
| `huri.question` | receives | prints the aggregated question |
| `huri.audio.out` | receives | plays the reply in order; optionally saves each utterance as WAV |
| `huri.motion` | receives | logs a bar per body part showing how much it moves; optionally saves each chunk as `.npz` |

The client config's `send:` / `subscribe:` lists pick among them; its `cli:`
section holds the device settings:

```yaml
cli:
  audio_in:  {sample_rate: 16000, frame_duration: 0.030}
  audio_out: {sample_rate: 44100, save_audio_dir: ""}   # "" = do not save
  motion:    {save_motion_dir: ""}                      # "" = do not save
```

The saved motion chunks (`poses`, `expressions`, `trans`, `fps`, `pts`) are
SMPL-X parameters and can be rendered offline with the EMAGE tooling.
