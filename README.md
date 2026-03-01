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


## Download

The current stable version of Nicotine+ is 3.3.10, released on March 10, 2025.
See the [release notes](NEWS.md).

Downloads are available for:

 - [GNU/Linux, *BSD, Haiku and Solaris](doc/DOWNLOADS.md#gnulinux-bsd-haiku-solaris)
 - [Windows](doc/DOWNLOADS.md#windows)
 - [macOS](doc/DOWNLOADS.md#macos)


## Get Involved

If you feel like contributing to Nicotine+, there are several ways to get
involved:

 - [Issue Tracker](https://github.com/nicotine-plus/nicotine-plus/issues)
     – Report a problem or suggest improvements
 - [Testing](doc/TESTING.md)
     – Download the latest unstable build and help test Nicotine+
 - [Translations](doc/TRANSLATIONS.md)
     – Translate Nicotine+ into another language with [Weblate](https://hosted.weblate.org/engage/nicotine-plus)
 - [Packaging](doc/PACKAGING.md)
     – Package Nicotine+ for a distribution or operating system
 - [Development](doc/DEVELOPING.md)
     – Implement bug fixes, enhancements or new features
 - [IRC Channel](https://web.libera.chat/?channel=#nicotine+)
     – Chat in the #nicotine+ IRC channel on [Libera.Chat](https://libera.chat/)


## Where did the name Nicotine come from?

> I was in a geeky mood and was browsing bash.org's QDB.  
I stumbled across this quote:  
>> **\<etc>** so tempting to release a product called 'nicotine' and wait for
>> the patches.  
>> **\<etc>** then i would have a reason to only apply one patch a day.
>> otherwise, i'm going against medical advise.  
>
> So I thought what the hell and bluntly stole etc's idea.

— <cite>Hyriand, former Nicotine maintainer, 2003</cite>


## Legal and Privacy

The Nicotine+ Team does not collect any data used or stored by the client.
Different policies may apply for data sent to the default Soulseek server,
which is not operated by the Nicotine+ Team.

When connecting to the default Soulseek server, you agree to abide by the
Soulseek [rules](https://www.slsknet.org/news/node/681) and
[terms of service](https://www.slsknet.org/news/node/682).

Soulseek is an unencrypted protocol not intended for secure communication.


## Authors

Nicotine+ is free and open source software, released under the terms of the
[GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0-standalone.html).
Nicotine+ exists thanks to its [authors](AUTHORS.md).

© 2001–2026 Nicotine+, Nicotine and PySoulSeek Contributors
