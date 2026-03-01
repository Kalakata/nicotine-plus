# SPDX-FileCopyrightText: 2025 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import threading

from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log

GSTREAMER_AVAILABLE = False

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    GSTREAMER_AVAILABLE = True
except Exception:
    pass

NUMPY_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:
    pass

LIBROSA_AVAILABLE = False

try:
    import librosa
    LIBROSA_AVAILABLE = True
except Exception:
    pass


# Known frequency cutoffs for common MP3 bitrates (approximate upper bound in Hz)
BITRATE_CUTOFFS = {
    32: 7000,
    48: 8500,
    64: 10000,
    80: 11500,
    96: 13500,
    112: 15000,
    128: 16000,
    160: 17500,
    192: 19000,
    224: 19500,
    256: 20000,
    320: 20500,
}

# Playback states
STATE_STOPPED = "stopped"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"


class MusicPlayer:

    __slots__ = ("_pipeline", "_playbin", "_volume_element",
                 "_current_file", "_state", "_position_timer_id",
                 "_background_thread", "_sample_rate", "_volume")

    def __init__(self):

        self._pipeline = None
        self._playbin = None
        self._volume_element = None
        self._current_file = None
        self._state = STATE_STOPPED
        self._position_timer_id = None
        self._background_thread = None
        self._sample_rate = 44100
        self._volume = config.sections.get("players", {}).get("volume", 100) / 100.0

        for event_name, callback in (
            ("quit", self._quit),
        ):
            events.connect(event_name, callback)

    @property
    def state(self):
        return self._state

    @property
    def current_file(self):
        return self._current_file

    def play(self, file_path):

        if not GSTREAMER_AVAILABLE:
            log.add("GStreamer not available, cannot play audio")
            return

        if not os.path.isfile(file_path):
            log.add("Music player: file not found: %s", file_path)
            return

        self.stop()

        self._current_file = file_path
        self._build_pipeline(file_path)

        if self._pipeline is None:
            return

        self._pipeline.set_state(Gst.State.PLAYING)
        self._state = STATE_PLAYING
        events.emit("music-player-state-changed", self._state, file_path)

        self._start_position_updates()

    def _build_pipeline(self, file_path):

        self._pipeline = Gst.Pipeline.new("music-player")

        # Source
        source = Gst.ElementFactory.make("filesrc", "source")
        source.set_property("location", file_path)

        # Decoder
        decoder = Gst.ElementFactory.make("decodebin", "decoder")

        # Audio convert + resample for playback
        audioconvert = Gst.ElementFactory.make("audioconvert", "convert")
        audioresample = Gst.ElementFactory.make("audioresample", "resample")

        # Volume control
        volume = Gst.ElementFactory.make("volume", "volume")
        volume.set_property("volume", self._volume)

        # Output
        sink = Gst.ElementFactory.make("autoaudiosink", "sink")

        if any(elem is None for elem in (source, decoder, audioconvert,
                                         audioresample, volume, sink)):
            log.add("Music player: failed to create GStreamer elements. "
                     "Check that gstreamer plugins are installed.")
            self._pipeline = None
            return

        self._volume_element = volume

        # Add elements to pipeline
        for elem in (source, decoder, audioconvert, audioresample, volume, sink):
            self._pipeline.add(elem)

        # Link source -> decoder (decoder pads are dynamic)
        source.link(decoder)

        # Link: audioconvert -> audioresample -> volume -> sink
        audioconvert.link(audioresample)
        audioresample.link(volume)
        volume.link(sink)

        # Handle dynamic pad from decodebin
        decoder.connect("pad-added", self._on_decoder_pad_added, audioconvert)

        # Set up bus message handling
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _on_decoder_pad_added(self, _decoder, pad, audioconvert):

        caps = pad.get_current_caps()
        if caps is None:
            caps = pad.query_caps(None)

        struct_obj = caps.get_structure(0)
        if struct_obj is None:
            return

        name = struct_obj.get_name()
        if not name.startswith("audio/"):
            return

        # Get sample rate from caps if available
        success, rate = struct_obj.get_int("rate")
        if success:
            self._sample_rate = rate

        sink_pad = audioconvert.get_static_pad("sink")
        if not sink_pad.is_linked():
            pad.link(sink_pad)

    def _on_bus_message(self, _bus, message):

        msg_type = message.type

        if msg_type == Gst.MessageType.EOS:
            self.stop()

        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.add("Music player error: %s", f"{err.message} ({debug})")
            self.stop()

    def pause(self):

        if self._pipeline is None or self._state != STATE_PLAYING:
            return

        self._pipeline.set_state(Gst.State.PAUSED)
        self._state = STATE_PAUSED
        self._stop_position_updates()
        events.emit("music-player-state-changed", self._state, self._current_file)

    def resume(self):

        if self._pipeline is None or self._state != STATE_PAUSED:
            return

        # Query current position before resuming
        success, position = self._pipeline.query_position(Gst.Format.TIME)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.add("Music player: failed to resume, rebuilding pipeline")
            # Rebuild pipeline and seek to saved position
            if self._current_file:
                pos_seconds = position / Gst.SECOND if success else 0
                self._state = STATE_STOPPED
                self.play(self._current_file)
                if pos_seconds > 0:
                    self.seek(pos_seconds)
            return

        # Flush-seek to current position to kick-start audio output
        if success and position > 0:
            self._pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                position
            )

        self._state = STATE_PLAYING
        self._start_position_updates()
        events.emit("music-player-state-changed", self._state, self._current_file)

    def toggle_pause(self):

        if self._state == STATE_PLAYING:
            self.pause()
        elif self._state == STATE_PAUSED:
            self.resume()

    def stop(self):

        self._stop_position_updates()

        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

        self._volume_element = None

        if self._state != STATE_STOPPED:
            self._state = STATE_STOPPED
            events.emit("music-player-state-changed", self._state, self._current_file)

    def seek(self, position_seconds):

        if self._pipeline is None:
            return

        position_ns = int(position_seconds * Gst.SECOND)
        self._pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            position_ns
        )

    def set_volume(self, volume):
        """Set volume from 0.0 to 1.0."""

        self._volume = max(0.0, min(1.0, volume))

        if self._volume_element is not None:
            self._volume_element.set_property("volume", self._volume)

    def get_position(self):
        """Returns (current_seconds, duration_seconds) or (0, 0) if unavailable."""

        if self._pipeline is None:
            return 0, 0

        success, position = self._pipeline.query_position(Gst.Format.TIME)
        if not success:
            position = 0

        success, duration = self._pipeline.query_duration(Gst.Format.TIME)
        if not success:
            duration = 0

        return position / Gst.SECOND, duration / Gst.SECOND

    def _start_position_updates(self):

        self._stop_position_updates()
        self._position_timer_id = events.schedule(
            delay=0.5,
            callback=self._emit_position,
            repeat=True
        )

    def _stop_position_updates(self):

        if self._position_timer_id is not None:
            events.cancel_scheduled(self._position_timer_id)
            self._position_timer_id = None

    def _emit_position(self):

        if self._state == STATE_STOPPED:
            return

        current, duration = self.get_position()
        events.emit("music-player-position-updated", current, duration)

    # Audio Decoding #

    @staticmethod
    def _decode_audio(file_path):
        """Decode audio file to mono numpy array at native sample rate.

        Uses librosa.load() which handles MP3, FLAC, WAV, OGG via
        soundfile + audioread backends.
        Returns (samples, sample_rate) or (None, None) on failure.
        """

        try:
            samples, sample_rate = librosa.load(file_path, sr=None, mono=True)
            return samples, sample_rate
        except Exception as e:
            log.add("Music player: failed to decode audio: %s", str(e))
            return None, None

    # Spectrogram Analysis #

    def analyze_and_generate_waveform(self, file_path, num_bars=150):
        """Run waveform generation then spectral analysis in a background thread."""

        if not LIBROSA_AVAILABLE:
            log.add("librosa not available, cannot analyze audio")
            return

        if not os.path.isfile(file_path):
            return

        if self._background_thread is not None and self._background_thread.is_alive():
            return

        self._background_thread = threading.Thread(
            target=self._run_background_tasks, args=(file_path, num_bars),
            name="AudioBackgroundThread", daemon=True
        )
        self._background_thread.start()

    def _run_background_tasks(self, file_path, num_bars):
        """Decode audio once, then generate waveform and spectrogram from numpy arrays."""

        samples, sample_rate = self._decode_audio(file_path)
        if samples is None:
            log.add("Music player: failed to decode audio for analysis")
            return

        # Abort if file changed or playback stopped
        if self._current_file != file_path or self._state == STATE_STOPPED:
            return

        self._generate_waveform(samples, sample_rate, file_path, num_bars)

        # Abort check again before heavier spectrogram work
        if self._current_file != file_path or self._state == STATE_STOPPED:
            return

        self._generate_spectrogram(samples, sample_rate, file_path)

    def _generate_waveform(self, samples, sample_rate, file_path, num_bars):
        """Generate waveform envelope from raw samples using numpy."""

        # Low-pass focus: downsample to ~1kHz to capture bass envelope
        target_rate = 1000
        if sample_rate > target_rate:
            ratio = sample_rate // target_rate
            samples_lp = samples[::ratio]
        else:
            samples_lp = samples

        total = len(samples_lp)
        if total == 0:
            return

        # Take absolute values for amplitude envelope
        abs_samples = np.abs(samples_lp)

        waveform = []
        for i in range(num_bars):
            start = int(i * total / num_bars)
            end = int((i + 1) * total / num_bars)
            end = max(end, start + 1)
            segment_max = float(np.max(abs_samples[start:end]))
            waveform.append(segment_max)

        # Normalize to 0.0-1.0
        max_val = max(waveform) if waveform else 1.0
        if max_val > 0:
            waveform = [v / max_val for v in waveform]

        events.emit_main_thread("music-player-waveform-ready", file_path, waveform)

    def _generate_spectrogram(self, samples, sample_rate, file_path):
        """Generate spectrogram using librosa STFT and run transcode detection."""

        # Compute STFT — n_fft=4096 gives ~10Hz resolution at 44.1kHz
        stft = librosa.stft(samples, n_fft=4096, hop_length=1024)
        magnitude = np.abs(stft)

        # Convert to dB
        spectrogram_db = librosa.amplitude_to_db(magnitude, ref=np.max)

        # librosa STFT shape is (n_fft/2+1, num_frames)
        # GUI expects (num_frames, num_bands) — transpose
        spectrogram = spectrogram_db.T.astype(np.float32)

        result = self._detect_transcode(spectrogram, sample_rate)

        events.emit_main_thread(
            "music-player-analysis-complete",
            file_path, spectrogram, result
        )

    def _detect_transcode(self, spectrogram, sample_rate):
        """Analyze spectrogram to detect frequency cutoff indicating a transcode.

        Detects the frequency where the spectrum drops to the dB floor,
        indicating a lossy codec's hard frequency cutoff.

        Returns dict with:
            cutoff_hz: detected frequency cutoff
            verdict: "genuine", "likely_transcode", or "inconclusive"
            estimated_source_bitrate: estimated original bitrate if transcode detected
        """

        num_bands = spectrogram.shape[1]
        nyquist = sample_rate / 2.0
        freq_per_band = nyquist / num_bands

        # Average magnitude across all time frames
        avg_spectrum = np.mean(spectrogram, axis=0)

        # Smooth the spectrum (500Hz window) to remove noise
        smooth_size = max(3, int(500 / freq_per_band))
        if smooth_size % 2 == 0:
            smooth_size += 1
        kernel = np.ones(smooth_size) / smooth_size
        smoothed = np.convolve(avg_spectrum, kernel, mode="same")

        # The dB floor from librosa.amplitude_to_db is -80dB
        # A lossy codec creates a hard cutoff where the spectrum drops to this floor
        # Walk from high frequencies down, find where spectrum rises above the floor
        db_floor = -79.0  # just above the -80dB floor
        search_lo = int(5000 / freq_per_band)
        search_hi = int((nyquist - 500) / freq_per_band)  # skip edge artifacts

        cutoff_band = search_hi
        for i in range(search_hi, search_lo, -1):
            if smoothed[i] > db_floor:
                cutoff_band = i
                break

        cutoff_hz = cutoff_band * freq_per_band

        # Determine verdict
        verdict = "genuine"
        estimated_source = None

        # Get reported bitrate from TinyTag
        from pynicotine.external.tinytag import TinyTag
        try:
            tag = TinyTag.get(self._current_file if self._current_file else "")
            reported_bitrate = int(tag.bitrate) if tag.bitrate else None
        except Exception:
            reported_bitrate = None

        # Determine expected cutoff based on reported bitrate
        # Lossless files (500+ kbps) should have content near Nyquist
        if reported_bitrate and reported_bitrate >= 500:
            expected_cutoff = nyquist * 0.95  # ~21kHz for 44.1kHz
        elif reported_bitrate and reported_bitrate >= 256:
            expected_cutoff = 20000
        else:
            expected_cutoff = 19000

        if cutoff_hz < expected_cutoff:
            # Find closest matching source bitrate
            best_match = None
            best_diff = float("inf")
            for bitrate, cutoff in BITRATE_CUTOFFS.items():
                diff = abs(cutoff_hz - cutoff)
                if diff < best_diff:
                    best_diff = diff
                    best_match = bitrate

            verdict = "likely_transcode"
            estimated_source = best_match

        return {
            "cutoff_hz": round(cutoff_hz),
            "verdict": verdict,
            "estimated_source_bitrate": estimated_source,
            "reported_bitrate": reported_bitrate,
            "sample_rate": sample_rate,
        }

    def _quit(self):

        self.stop()

        if self._background_thread is not None and self._background_thread.is_alive():
            self._background_thread.join(timeout=2)
