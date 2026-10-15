# huri-events

The curated HuRI event vocabulary: one event class per `huri.*` topic, with its
wire format, registered through the `huri.events` entry point group. The
`huri.` namespace is reserved for this package; third-party events use their
own (`acme.*`), see the core README.

It is deliberately light (huri-core and numpy): a client installs it next to
`huri-core[client]` to decode every curated topic.

```sh
pip install "huri-core[client]" ./packages/huri-events      # from a HuRI checkout
```

This directory is a self-contained distribution, laid out to move as-is to its
own `huri-events` repository.
