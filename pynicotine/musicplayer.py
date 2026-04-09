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

    __slots__ = ("_pipeline", "_volume_element",
                 "_current_file", "_state", "_position_timer_id",
                 "_background_thread", "_sample_rate", "_volume",
                 "_lock", "_cancel_event")

    def __init__(self):

        self._pipeline = None
        self._volume_element = None
        self._current_file = None
        self._state = STATE_STOPPED
        self._position_timer_id = None
        self._background_thread = None
        self._sample_rate = 44100
        self._volume = config.sections.get("players", {}).get("volume", 100) / 100.0
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()

        events.connect("quit", self._quit)

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

        with self._lock:
            self._current_file = file_path
        self._build_pipeline(file_path)

        if self._pipeline is None:
            return

        self._pipeline.set_state(Gst.State.PLAYING)
        with self._lock:
            self._state = STATE_PLAYING
        events.emit("music-player-state-changed", self._state, file_path)

        self._start_position_updates()

    def _build_pipeline(self, file_path):

        self._pipeline = Gst.Pipeline.new("music-player")

        # Source
        source = Gst.ElementFactory.make("filesrc", "source")

        # Decoder
        decoder = Gst.ElementFactory.make("decodebin", "decoder")

        # Audio convert + resample for playback
        audioconvert = Gst.ElementFactory.make("audioconvert", "convert")
        audioresample = Gst.ElementFactory.make("audioresample", "resample")

        # Volume control
        volume = Gst.ElementFactory.make("volume", "volume")

        # Output
        sink = Gst.ElementFactory.make("autoaudiosink", "sink")

        if any(elem is None for elem in (source, decoder, audioconvert,
                                         audioresample, volume, sink)):
            log.add("Music player: failed to create GStreamer elements. "
                     "Check that gstreamer plugins are installed.")
            self._pipeline = None
            return

        source.set_property("location", file_path)
        volume.set_property("volume", self._volume)
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
        decoder.connect("autoplug-continue", self._on_autoplug_continue)

        # Set up bus message handling
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    @staticmethod
    def _on_autoplug_continue(_decoder, _pad, caps):
        """Skip non-audio metadata streams (e.g. APE tags) to avoid
        missing plugin errors."""

        struct = caps.get_structure(0)
        if struct is not None and struct.get_name() == "application/x-apetag":
            return False

        return True

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
            events.emit_main_thread("music-player-track-ended")

        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            debug_str = debug or ""

            if "Missing" in debug_str and "plugin" in err.message.lower():
                log.add("Music player: missing GStreamer plugin — %s. "
                        "Install gst-plugins-good for broader format support.", debug_str)
            else:
                log.add("Music player error: %s", f"{err.message} ({debug_str})")

            self.stop()

    def pause(self):

        if self._pipeline is None or self._state != STATE_PLAYING:
            return

        self._pipeline.set_state(Gst.State.PAUSED)
        with self._lock:
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

        with self._lock:
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

        with self._lock:
            was_stopped = self._state == STATE_STOPPED
            self._state = STATE_STOPPED
        if not was_stopped:
            events.emit_main_thread("music-player-state-changed", self._state, self._current_file)

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
        """Decode audio file to mono float32 numpy array using GStreamer.

        Builds a decode-only pipeline: filesrc -> decodebin -> audioconvert
        -> capsfilter (mono F32LE) -> appsink.
        Returns (samples, sample_rate) or (None, None) on failure.
        """

        pipeline = Gst.Pipeline.new("decoder")

        filesrc = Gst.ElementFactory.make("filesrc", None)
        decodebin = Gst.ElementFactory.make("decodebin", None)
        audioconvert = Gst.ElementFactory.make("audioconvert", None)
        audioresample = Gst.ElementFactory.make("audioresample", None)
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        appsink = Gst.ElementFactory.make("appsink", None)

        if any(e is None for e in (filesrc, decodebin, audioconvert,
                                   audioresample, capsfilter, appsink)):
            log.add("Music player: failed to create GStreamer decode elements")
            return None, None

        filesrc.set_property("location", file_path)
        capsfilter.set_property("caps", Gst.Caps.from_string(
            "audio/x-raw,format=F32LE,channels=1"
        ))
        appsink.set_property("sync", False)

        for elem in (filesrc, decodebin, audioconvert,
                     audioresample, capsfilter, appsink):
            pipeline.add(elem)

        filesrc.link(decodebin)
        audioconvert.link(audioresample)
        audioresample.link(capsfilter)
        capsfilter.link(appsink)

        def _on_pad_added(_decodebin, pad):
            caps = pad.get_current_caps() or pad.query_caps(None)
            struct = caps.get_structure(0) if caps else None
            if struct and struct.get_name().startswith("audio/"):
                sink_pad = audioconvert.get_static_pad("sink")
                if not sink_pad.is_linked():
                    pad.link(sink_pad)

        decodebin.connect("pad-added", _on_pad_added)
        decodebin.connect("autoplug-continue", MusicPlayer._on_autoplug_continue)

        pipeline.set_state(Gst.State.PLAYING)

        buffers = []
        sample_rate = 44100
        rate_detected = False

        try:
            while True:
                sample = appsink.emit("pull-sample")
                if sample is None:
                    break

                if not rate_detected:
                    sample_caps = sample.get_caps()
                    if sample_caps and sample_caps.get_size() > 0:
                        struct = sample_caps.get_structure(0)
                        success, rate = struct.get_int("rate")
                        if success:
                            sample_rate = rate
                            rate_detected = True

                buf = sample.get_buffer()
                success, map_info = buf.map(Gst.MapFlags.READ)
                if success:
                    buffers.append(
                        np.frombuffer(bytes(map_info.data), dtype=np.float32).copy()
                    )
                    buf.unmap(map_info)
        except Exception as e:
            log.add("Music player: failed to decode audio: %s", str(e))
            pipeline.set_state(Gst.State.NULL)
            return None, None

        pipeline.set_state(Gst.State.NULL)

        if not buffers:
            return None, None

        return np.concatenate(buffers), sample_rate

    # Spectrogram Analysis #

    def analyze_and_generate_waveform(self, file_path, num_bars=150):
        """Run waveform generation then spectral analysis in a background thread."""

        if not GSTREAMER_AVAILABLE or not NUMPY_AVAILABLE:
            log.add("GStreamer/numpy not available, cannot analyze audio")
            return

        if not os.path.isfile(file_path):
            return

        # Cancel any running analysis
        self._cancel_event.set()
        if self._background_thread is not None and self._background_thread.is_alive():
            self._background_thread.join(timeout=3)

        self._cancel_event = threading.Event()
        cancel = self._cancel_event

        self._background_thread = threading.Thread(
            target=self._run_background_tasks, args=(file_path, num_bars, cancel),
            name="AudioBackgroundThread", daemon=True
        )
        self._background_thread.start()

    def _is_cancelled(self, cancel, file_path):
        """Check if this analysis should abort."""

        if cancel.is_set():
            return True
        with self._lock:
            return self._current_file != file_path

    def _run_background_tasks(self, file_path, num_bars, cancel):
        """Decode audio once, then generate waveform and spectrogram from numpy arrays."""

        samples, sample_rate = self._decode_audio(file_path)
        if samples is None:
            log.add("Music player: failed to decode audio for analysis")
            return

        if self._is_cancelled(cancel, file_path):
            return

        self._generate_waveform(samples, sample_rate, file_path, num_bars)

        if self._is_cancelled(cancel, file_path):
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

    @staticmethod
    def _stft(samples, n_fft=4096, hop_length=1024):
        """Compute Short-Time Fourier Transform using numpy.

        Returns complex matrix of shape (n_fft//2 + 1, num_frames).
        """

        window = np.hanning(n_fft)
        num_frames = 1 + (len(samples) - n_fft) // hop_length
        stft_matrix = np.empty((n_fft // 2 + 1, num_frames), dtype=np.complex64)

        for i in range(num_frames):
            start = i * hop_length
            stft_matrix[:, i] = np.fft.rfft(samples[start:start + n_fft] * window)

        return stft_matrix

    @staticmethod
    def _amplitude_to_db(magnitude, top_db=80.0):
        """Convert amplitude spectrogram to dB scale (mirrors librosa behaviour)."""

        amin = 1e-5
        ref_value = np.max(magnitude)
        if ref_value < amin:
            ref_value = amin

        log_spec = 20.0 * np.log10(np.maximum(magnitude, amin))
        log_spec -= 20.0 * np.log10(max(ref_value, amin))

        if top_db is not None:
            log_spec = np.maximum(log_spec, log_spec.max() - top_db)

        return log_spec

    def _generate_spectrogram(self, samples, sample_rate, file_path):
        """Generate spectrogram using numpy STFT and run transcode detection."""

        # Compute STFT — n_fft=4096 gives ~10Hz resolution at 44.1kHz
        stft = self._stft(samples, n_fft=4096, hop_length=1024)
        magnitude = np.abs(stft)

        # Convert to dB
        spectrogram_db = self._amplitude_to_db(magnitude)

        # STFT shape is (n_fft/2+1, num_frames)
        # GUI expects (num_frames, num_bands) — transpose
        spectrogram = spectrogram_db.T.astype(np.float32)

        result = self._detect_transcode(spectrogram, sample_rate, file_path)

        events.emit_main_thread(
            "music-player-analysis-complete",
            file_path, spectrogram, result
        )

    def _detect_transcode(self, spectrogram, sample_rate, file_path):
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

        # The dB floor from _amplitude_to_db is -80dB (top_db=80)
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
            tag = TinyTag.get(file_path)
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
            estimated_source = min(BITRATE_CUTOFFS, key=lambda br: abs(cutoff_hz - BITRATE_CUTOFFS[br]))
            verdict = "likely_transcode"

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
