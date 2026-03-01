<!--
  SPDX-FileCopyrightText: 2013-2026 Nicotine+ Contributors
  SPDX-License-Identifier: GPL-3.0-or-later
-->

# Nicotine+ (Fork)

<img src="data/icons/icon.svg" alt="Nicotine+ Logo" align="right"
 width="128" height="128">

A fork of [Nicotine+](https://github.com/nicotine-plus/nicotine-plus), the
graphical client for the [Soulseek](https://www.slsknet.org/news/) peer-to-peer
network, with an integrated music player and audio analysis tools.

Share files, chat, and find people with similar interests. Nicotine+
is user-friendly, fast, free, and open source. It provides features
and refinements that focus on usability, while remaining fully
compatible with other Soulseek clients.

Nicotine+ is written in Python, and uses GTK for its graphical user
interface.


## Fork Features

### Integrated Music Player
A sidebar music player powered by GStreamer for previewing your shared files
directly within the client.

- **Local file browser** — browse and play audio files from your shared folders
- **Folder monitoring** — automatically detects changes in your music library
- **Playback controls** — play, pause, next, previous, and volume control
- **Waveform seek bar** — visual waveform display with click-to-seek

### Spectrogram Analyzer
Real-time spectral analysis for evaluating audio quality, powered by librosa
and numpy.

- **Full spectrogram display** — frequency vs. time visualization (Spek-style)
- **Transcode detection** — automatically identifies lossy-to-lossless transcodes
  by detecting frequency cutoffs
- **Quality scoring** — rates audio quality based on spectral analysis
- **Bitrate reference lines** — visual guides for common MP3 bitrate cutoffs
  (128k, 192k, 320k)
- **Zoom and pan** — scroll to zoom into frequency ranges, drag to pan


## Building

### Requirements
- Python 3.9+
- GTK 4 / PyGObject
- GStreamer (for music player)
- numpy, librosa (for spectrogram analysis)

### Run from source
```bash
brew install gtk4 pygobject3 gstreamer gst-plugins-base gst-plugins-good
pip install numpy librosa
python3 -m pynicotine
```

### Build macOS .dmg
```bash
python3 build-aux/macos/dependencies.py
cd build-aux/macos && python3 setup.py bdist_dmg
```

The `.dmg` will be in `build-aux/macos/build/`.


## Based on

This is a fork of [Nicotine+](https://github.com/nicotine-plus/nicotine-plus).
All original Nicotine+ features (file sharing, chat, search, transfers) are
fully preserved.

Licensed under the
[GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0-standalone.html).
