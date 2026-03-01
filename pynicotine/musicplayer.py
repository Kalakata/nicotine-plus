# SPDX-FileCopyrightText: 2025 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import struct
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
except (ImportError, ValueError):
    pass

NUMPY_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    pass


# Known frequency cutoffs for common MP3 bitrates (approximate upper bound in Hz)
BITRATE_CUTOFFS = {
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

    __slots__ = ("_pipeline", "_playbin", "_spectrum_element", "_volume_element",
                 "_current_file", "_state", "_position_timer_id",
                 "_analysis_pipeline", "_analysis_thread", "_spectrogram_data",
                 "_sample_rate", "_volume")

    def __init__(self):

        self._pipeline = None
        self._playbin = None
        self._spectrum_element = None
        self._volume_element = None
        self._current_file = None
        self._state = STATE_STOPPED
        self._position_timer_id = None
        self._analysis_pipeline = None
        self._analysis_thread = None
        self._spectrogram_data = []
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

        # Tee to split audio into playback + analysis
        tee = Gst.ElementFactory.make("tee", "tee")

        # Playback branch
        queue_play = Gst.ElementFactory.make("queue", "queue_play")
        sink = Gst.ElementFactory.make("autoaudiosink", "sink")

        # Analysis branch
        queue_analysis = Gst.ElementFactory.make("queue", "queue_analysis")
        audioconvert2 = Gst.ElementFactory.make("audioconvert", "convert2")
        spectrum = Gst.ElementFactory.make("spectrum", "spectrum")
        fakesink = Gst.ElementFactory.make("fakesink", "fakesink")

        if any(elem is None for elem in (source, decoder, audioconvert, audioresample,
                                         volume, tee, queue_play, sink, queue_analysis,
                                         audioconvert2, spectrum, fakesink)):
            log.add("Music player: failed to create GStreamer elements. "
                     "Check that gstreamer plugins are installed.")
            self._pipeline = None
            return

        # Configure spectrum element
        spectrum.set_property("bands", 512)
        spectrum.set_property("interval", 100000000)  # 100ms
        spectrum.set_property("threshold", -80)
        spectrum.set_property("post-messages", True)
        spectrum.set_property("message-magnitude", True)
        self._spectrum_element = spectrum
        self._volume_element = volume

        # Add elements to pipeline
        for elem in (source, decoder, audioconvert, audioresample, volume, tee,
                     queue_play, sink, queue_analysis, audioconvert2, spectrum, fakesink):
            self._pipeline.add(elem)

        # Link source -> decoder (decoder pads are dynamic)
        source.link(decoder)

        # Link playback branch: convert -> resample -> volume -> tee
        audioconvert.link(audioresample)
        audioresample.link(volume)
        volume.link(tee)

        # Link tee -> playback queue -> sink
        tee.link(queue_play)
        queue_play.link(sink)

        # Link tee -> analysis queue -> convert2 -> spectrum -> fakesink
        tee.link(queue_analysis)
        queue_analysis.link(audioconvert2)
        audioconvert2.link(spectrum)
        spectrum.link(fakesink)

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
            log.add("Music player error: %s (%s)", err.message, debug)
            self.stop()

        elif msg_type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure and structure.get_name() == "spectrum":
                self._handle_spectrum_message(structure)

    def _handle_spectrum_message(self, structure):

        magnitudes = structure.get_value("magnitude")
        if magnitudes is None:
            return

        # Convert GValueArray to list
        mag_list = [magnitudes[i] for i in range(len(magnitudes))]

        events.emit_main_thread("music-player-spectrum-data", mag_list)

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

        self._pipeline.set_state(Gst.State.PLAYING)
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

        self._spectrum_element = None
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

    # Spectrogram Analysis #

    def analyze_file(self, file_path):
        """Run full-file spectral analysis in a background thread."""

        if not GSTREAMER_AVAILABLE:
            log.add("GStreamer not available, cannot analyze audio")
            return

        if not NUMPY_AVAILABLE:
            log.add("NumPy not available, cannot analyze audio")
            return

        if not os.path.isfile(file_path):
            return

        if self._analysis_thread is not None and self._analysis_thread.is_alive():
            return

        self._analysis_thread = threading.Thread(
            target=self._run_analysis, args=(file_path,),
            name="AudioAnalysisThread", daemon=True
        )
        self._analysis_thread.start()

    def _run_analysis(self, file_path):

        spectrogram_frames = []
        sample_rate = 44100
        analysis_done = threading.Event()

        pipeline = Gst.Pipeline.new("analysis")

        source = Gst.ElementFactory.make("filesrc", "src")
        source.set_property("location", file_path)

        decoder = Gst.ElementFactory.make("decodebin", "dec")
        audioconvert = Gst.ElementFactory.make("audioconvert", "conv")

        spectrum = Gst.ElementFactory.make("spectrum", "spectrum")
        spectrum.set_property("bands", 512)
        spectrum.set_property("interval", 50000000)  # 50ms for finer time resolution
        spectrum.set_property("threshold", -80)
        spectrum.set_property("post-messages", True)
        spectrum.set_property("message-magnitude", True)

        fakesink = Gst.ElementFactory.make("fakesink", "sink")

        for elem in (source, decoder, audioconvert, spectrum, fakesink):
            pipeline.add(elem)

        source.link(decoder)
        audioconvert.link(spectrum)
        spectrum.link(fakesink)

        def on_pad_added(_dec, pad, conv):
            caps = pad.get_current_caps() or pad.query_caps(None)
            struct_obj = caps.get_structure(0)
            if struct_obj and struct_obj.get_name().startswith("audio/"):
                nonlocal sample_rate
                success, rate = struct_obj.get_int("rate")
                if success:
                    sample_rate = rate
                sink_pad = conv.get_static_pad("sink")
                if not sink_pad.is_linked():
                    pad.link(sink_pad)

        decoder.connect("pad-added", on_pad_added, audioconvert)

        bus = pipeline.get_bus()
        bus.add_signal_watch()

        def on_message(_bus, message):
            if message.type == Gst.MessageType.EOS:
                analysis_done.set()
            elif message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                log.add("Music player analysis error: %s (%s)", err.message, debug)
                analysis_done.set()
            elif message.type == Gst.MessageType.ELEMENT:
                structure = message.get_structure()
                if structure and structure.get_name() == "spectrum":
                    magnitudes = structure.get_value("magnitude")
                    if magnitudes is not None:
                        frame = [magnitudes[i] for i in range(len(magnitudes))]
                        spectrogram_frames.append(frame)

        bus.connect("message", on_message)

        pipeline.set_state(Gst.State.PLAYING)
        analysis_done.wait(timeout=300)  # 5 minute max
        pipeline.set_state(Gst.State.NULL)

        if not spectrogram_frames:
            return

        spectrogram = np.array(spectrogram_frames, dtype=np.float32)
        result = self._detect_transcode(spectrogram, sample_rate)

        events.emit_main_thread(
            "music-player-analysis-complete",
            file_path, spectrogram, result
        )

    def _detect_transcode(self, spectrogram, sample_rate):
        """Analyze spectrogram to detect frequency cutoff indicating a transcode.

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

        # Find the peak magnitude in the audible range (1kHz - 10kHz)
        low_band = int(1000 / freq_per_band)
        high_band = int(10000 / freq_per_band)
        peak_magnitude = np.max(avg_spectrum[low_band:high_band])

        # Walk from high frequencies downward to find cutoff
        # Cutoff = where energy drops more than 20dB below peak consistently
        threshold = peak_magnitude - 25  # dB below peak
        cutoff_band = num_bands - 1

        # Use a sliding window of 5 bands for robustness
        window_size = 5
        for i in range(num_bands - window_size, low_band, -1):
            window_avg = np.mean(avg_spectrum[i:i + window_size])
            if window_avg > threshold:
                cutoff_band = i + window_size
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

        if cutoff_hz < 19000:
            # Find closest matching source bitrate
            best_match = None
            best_diff = float("inf")
            for bitrate, cutoff in BITRATE_CUTOFFS.items():
                diff = abs(cutoff_hz - cutoff)
                if diff < best_diff:
                    best_diff = diff
                    best_match = bitrate

            if reported_bitrate and reported_bitrate >= 256 and cutoff_hz < 19000:
                verdict = "likely_transcode"
                estimated_source = best_match
            elif cutoff_hz < 17000:
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

        if self._analysis_thread is not None and self._analysis_thread.is_alive():
            self._analysis_thread.join(timeout=2)
