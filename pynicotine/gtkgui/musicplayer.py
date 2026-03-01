# SPDX-FileCopyrightText: 2025 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import math
import os
import time

from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.filechooser import FileChooser
from pynicotine.gtkgui.widgets.treeview import TreeView
from pynicotine.logfacility import log
from pynicotine.musicplayer import GSTREAMER_AVAILABLE
from pynicotine.musicplayer import NUMPY_AVAILABLE
from pynicotine.shares import FileTypes


AUDIO_EXTENSIONS = {"." + ext for ext in FileTypes.AUDIO}


class MusicPlayerPanel:

    def __init__(self, window):

        (
            self.choose_folder_button,
            self.container,
            self.controls_box,
            self.duration_label,
            self.file_browser_box,
            self.file_list_container,
            self.header_box,
            self.next_button,
            self.now_playing_box,
            self.play_button,
            self.position_label,
            self.prev_button,
            self.seek_box,
            self.seek_scale,
            self.spectrogram_area,
            self.spectrogram_box,
            self.stop_button,
            self.track_artist_label,
            self.track_title_label,
            self.verdict_label,
            self.volume_box,
            self.volume_scale
        ) = ui.load(scope=self, path="musicplayer.ui")

        self.window = window
        self._current_folder = None
        self._file_list = []      # ordered list of audio file paths in current folder
        self._current_index = -1  # index in _file_list of currently playing file
        self._seeking = False     # True while user is dragging the seek bar
        self._spectrogram = None  # numpy 2D array of spectrogram data
        self._analysis_result = None

        # Append our container into the mainwindow's music_player_container
        if GTK_API_VERSION >= 4:
            window.music_player_container.append(self.container)
        else:
            window.music_player_container.add(self.container)

        # File list TreeView
        self.file_list_view = TreeView(
            window, parent=self.file_list_container, name="music_player_files",
            activate_row_callback=self.on_file_activated,
            persistent_widths=True,
            columns={
                "icon": {
                    "column_type": "icon",
                    "title": "",
                    "width": 25,
                    "hide_header": True
                },
                "filename": {
                    "column_type": "text",
                    "title": _("Name"),
                    "width": 200,
                    "expand_column": True,
                    "default_sort_type": "ascending",
                    "iterator_key": True
                },
                "bitrate": {
                    "column_type": "text",
                    "title": _("kbps"),
                    "width": 50
                },
                "duration": {
                    "column_type": "text",
                    "title": _("Length"),
                    "width": 45
                },
                "date": {
                    "column_type": "text",
                    "title": _("Date"),
                    "width": 70
                },
                "path_data": {
                    "data_type": GObject.TYPE_STRING
                }
            }
        )

        # Spectrogram drawing
        if GTK_API_VERSION >= 4:
            self.spectrogram_area.set_draw_func(self._draw_spectrogram)
        else:
            self.spectrogram_area.connect("draw", self._draw_spectrogram_gtk3)

        # Connect events
        for event_name, callback in (
            ("music-player-state-changed", self._on_state_changed),
            ("music-player-position-updated", self._on_position_updated),
            ("music-player-spectrum-data", self._on_spectrum_data),
            ("music-player-analysis-complete", self._on_analysis_complete),
        ):
            events.connect(event_name, callback)

        # Load initial folder
        self._set_initial_folder()

    def _set_initial_folder(self):

        folder = config.sections.get("transfers", {}).get("downloaddir", "")

        if not folder or not os.path.isdir(folder):
            folder = os.path.expanduser("~")

        self._load_folder(folder)

    def _load_folder(self, folder_path):

        if not os.path.isdir(folder_path):
            return

        self._current_folder = folder_path
        self._file_list.clear()
        self.file_list_view.clear()

        try:
            entries = sorted(os.listdir(folder_path))
        except OSError:
            return

        for entry in entries:
            full_path = os.path.join(folder_path, entry)

            if os.path.isdir(full_path):
                mtime = self._get_file_date(full_path)
                self.file_list_view.add_row(
                    [
                        "folder-symbolic",    # icon
                        entry,                 # filename
                        "",                    # bitrate
                        "",                    # duration
                        mtime,                 # date
                        full_path              # path_data
                    ],
                    select_row=False
                )
                continue

            _name, ext = os.path.splitext(entry)
            if ext.lower() not in AUDIO_EXTENSIONS:
                continue

            # Read metadata with TinyTag
            bitrate_str = ""
            duration_str = ""

            try:
                from pynicotine.external.tinytag import TinyTag
                tag = TinyTag.get(full_path)

                if tag.bitrate:
                    br = int(round(tag.bitrate))
                    vbr = " VBR" if getattr(tag, "is_vbr", False) else ""
                    bitrate_str = f"{br}{vbr}"

                if tag.duration:
                    mins = int(tag.duration // 60)
                    secs = int(tag.duration % 60)
                    duration_str = f"{mins}:{secs:02d}"
            except Exception:
                pass

            mtime = self._get_file_date(full_path)
            self._file_list.append(full_path)
            self.file_list_view.add_row(
                [
                    "emblem-documents-symbolic",  # icon
                    entry,                        # filename
                    bitrate_str,                  # bitrate
                    duration_str,                 # duration
                    mtime,                        # date
                    full_path                     # path_data
                ],
                select_row=False
            )

    # Signal Handlers (from UI) #

    def on_choose_folder(self, *_args):

        FileChooser(
            parent=self.window.widget,
            callback=self._on_folder_chosen,
            title=_("Choose Music Folder"),
            select_multiple=False,
            action="select_folder"
        )

    def _on_folder_chosen(self, selected, *_args):

        if selected:
            folder = selected if isinstance(selected, str) else selected[0]
            self._load_folder(folder)

    def on_file_activated(self, _list_view, _iterator, _column_id):

        iterator = self.file_list_view.get_selected_rows()
        if not iterator:
            return

        for row_iter in iterator:
            file_path = self.file_list_view.get_row_value(row_iter, "path_data")
            break

        if not file_path:
            return

        # If it's a directory, navigate into it
        if os.path.isdir(file_path):
            self._load_folder(file_path)
            return

        # Play the file
        if core.musicplayer is not None:
            core.musicplayer.play(file_path)

            # Update current index
            try:
                self._current_index = self._file_list.index(file_path)
            except ValueError:
                self._current_index = -1

    def on_play_pause(self, *_args):

        if core.musicplayer is None:
            return

        if core.musicplayer.state == "stopped":
            # Play first/selected file
            if self._current_index >= 0 and self._current_index < len(self._file_list):
                core.musicplayer.play(self._file_list[self._current_index])
            elif self._file_list:
                self._current_index = 0
                core.musicplayer.play(self._file_list[0])
        else:
            core.musicplayer.toggle_pause()

    def on_stop(self, *_args):

        if core.musicplayer is not None:
            core.musicplayer.stop()

    def on_previous(self, *_args):

        if core.musicplayer is None or not self._file_list:
            return

        self._current_index = max(0, self._current_index - 1)
        core.musicplayer.play(self._file_list[self._current_index])

    def on_next(self, *_args):

        if core.musicplayer is None or not self._file_list:
            return

        self._current_index = min(len(self._file_list) - 1, self._current_index + 1)
        core.musicplayer.play(self._file_list[self._current_index])

    def on_seek_changed(self, _scale, _scroll_type, value):

        if core.musicplayer is not None:
            core.musicplayer.seek(value)

        return False

    def on_volume_changed(self, scale):

        value = scale.get_value() / 100.0

        if core.musicplayer is not None:
            # Set volume directly on the GStreamer element — no scheduling overhead
            core.musicplayer.set_volume(value)

        return False

    # Event Callbacks #

    def _on_state_changed(self, state, file_path):

        if state == "playing":
            self.play_button.set_icon_name("media-playback-pause-symbolic")
            self._update_track_info(file_path)

            # Auto-analyze on play
            if file_path and core.musicplayer is not None:
                self.verdict_label.set_text(_("Analyzing..."))
                self._spectrogram = None
                self._analysis_result = None
                self.spectrogram_area.queue_draw()
                core.musicplayer.analyze_file(file_path)

        elif state == "paused":
            self.play_button.set_icon_name("media-playback-start-symbolic")
        elif state == "stopped":
            self.play_button.set_icon_name("media-playback-start-symbolic")
            self.position_label.set_text("0:00")
            self.duration_label.set_text("0:00")
            self.seek_scale.get_adjustment().set_value(0)

    def _update_track_info(self, file_path):

        if not file_path:
            return

        filename = os.path.basename(file_path)
        title = filename
        artist = ""

        try:
            from pynicotine.external.tinytag import TinyTag
            tag = TinyTag.get(file_path)

            if tag.title:
                title = tag.title
            if tag.artist:
                artist = tag.artist
        except Exception:
            pass

        self.track_title_label.set_text(title)
        self.track_artist_label.set_text(artist)

    def _on_position_updated(self, current, duration):

        self.position_label.set_text(self._format_time(current))
        self.duration_label.set_text(self._format_time(duration))

        adjustment = self.seek_scale.get_adjustment()
        adjustment.set_upper(max(duration, 1))
        adjustment.set_value(current)

    def _on_spectrum_data(self, magnitudes):
        # Real-time spectrum data during playback (could be used for live visualizer)
        pass

    def _on_analysis_complete(self, file_path, spectrogram, result):

        self._spectrogram = spectrogram
        self._analysis_result = result

        # Update verdict label
        verdict = result.get("verdict", "inconclusive")
        cutoff = result.get("cutoff_hz", 0)
        reported = result.get("reported_bitrate")
        estimated = result.get("estimated_source_bitrate")

        if verdict == "genuine":
            text = _("Genuine %(bitrate)s kbps (frequency content up to %(cutoff)s Hz)") % {
                "bitrate": reported or "?",
                "cutoff": cutoff
            }
        elif verdict == "likely_transcode":
            text = _("Likely transcode from ~%(source)s kbps (cutoff at %(cutoff)s Hz, "
                     "reported as %(reported)s kbps)") % {
                "source": estimated or "?",
                "cutoff": cutoff,
                "reported": reported or "?"
            }
        else:
            text = _("Inconclusive (cutoff at %(cutoff)s Hz)") % {"cutoff": cutoff}

        self.verdict_label.set_text(text)
        self.spectrogram_area.queue_draw()

    # Spectrogram Drawing #

    def _draw_spectrogram_gtk3(self, widget, cr):
        """GTK 3 draw signal handler."""

        allocation = widget.get_allocation()
        self._render_spectrogram(cr, allocation.width, allocation.height)

    def _draw_spectrogram(self, area, cr, width, height):
        """GTK 4 draw function."""

        self._render_spectrogram(cr, width, height)

    def _render_spectrogram(self, cr, width, height):
        """Render spectrogram using Cairo."""

        # Background
        cr.set_source_rgb(0.1, 0.1, 0.15)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if self._spectrogram is None or not NUMPY_AVAILABLE:
            return

        import numpy as np

        spectrogram = self._spectrogram
        num_frames = spectrogram.shape[0]
        num_bands = spectrogram.shape[1]

        if num_frames == 0 or num_bands == 0:
            return

        # Normalize spectrogram to 0-1 range for coloring
        min_val = spectrogram.min()
        max_val = spectrogram.max()
        value_range = max_val - min_val

        if value_range == 0:
            return

        # Draw frequency axis labels on the right
        label_width = 35
        plot_width = width - label_width
        plot_height = height - 15  # Leave room for time axis at bottom

        if plot_width <= 0 or plot_height <= 0:
            return

        # Draw spectrogram pixels
        x_step = max(1, plot_width / num_frames)
        y_step = max(0.5, plot_height / num_bands)

        for col in range(min(num_frames, int(plot_width / x_step) + 1)):
            frame_idx = int(col * num_frames / (plot_width / x_step + 1))
            if frame_idx >= num_frames:
                break

            for row in range(num_bands):
                val = (spectrogram[frame_idx, row] - min_val) / value_range

                # Color map: dark blue -> cyan -> yellow -> white
                r, g, b = self._spectrogram_color(val)
                cr.set_source_rgb(r, g, b)

                x = col * x_step
                y = plot_height - (row + 1) * y_step  # flip so low freq at bottom

                cr.rectangle(x, y, math.ceil(x_step), math.ceil(y_step))
                cr.fill()

        # Draw cutoff line if analysis result available
        if self._analysis_result:
            cutoff_hz = self._analysis_result.get("cutoff_hz", 0)
            sample_rate = self._analysis_result.get("sample_rate", 44100)
            nyquist = sample_rate / 2.0

            if 0 < cutoff_hz < nyquist:
                cutoff_ratio = cutoff_hz / nyquist
                cutoff_y = plot_height - (cutoff_ratio * plot_height)

                cr.set_source_rgba(1.0, 0.2, 0.2, 0.8)
                cr.set_line_width(1.5)
                cr.move_to(0, cutoff_y)
                cr.line_to(plot_width, cutoff_y)
                cr.stroke()

                # Label
                cr.set_font_size(9)
                label = f"{cutoff_hz} Hz"
                cr.move_to(3, cutoff_y - 3)
                cr.show_text(label)

        # Draw frequency axis labels
        cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.set_font_size(8)
        sample_rate = 44100
        if self._analysis_result:
            sample_rate = self._analysis_result.get("sample_rate", 44100)
        nyquist = sample_rate / 2.0

        for freq_khz in (1, 5, 10, 15, 20):
            freq_hz = freq_khz * 1000
            if freq_hz > nyquist:
                break
            ratio = freq_hz / nyquist
            y = plot_height - (ratio * plot_height)
            cr.move_to(plot_width + 2, y + 3)
            cr.show_text(f"{freq_khz}k")

    @staticmethod
    def _spectrogram_color(value):
        """Map a 0-1 value to a spectrogram color."""

        if value < 0.25:
            t = value / 0.25
            return (0.0, 0.0, 0.2 + 0.6 * t)            # dark -> blue
        elif value < 0.5:
            t = (value - 0.25) / 0.25
            return (0.0, t, 0.8)                           # blue -> cyan
        elif value < 0.75:
            t = (value - 0.5) / 0.25
            return (t, 0.8 + 0.2 * t, 0.8 - 0.8 * t)     # cyan -> yellow
        else:
            t = (value - 0.75) / 0.25
            return (1.0, 1.0, t)                            # yellow -> white

    # Utilities #

    @staticmethod
    def _get_file_date(file_path):
        """Get file modification date as a short string."""

        try:
            mtime = os.path.getmtime(file_path)
            return time.strftime("%m/%d/%y", time.localtime(mtime))
        except OSError:
            return ""

    @staticmethod
    def _format_time(seconds):
        """Format seconds as m:ss."""

        if seconds <= 0 or math.isinf(seconds) or math.isnan(seconds):
            return "0:00"

        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}:{secs:02d}"

    def toggle_visible(self):
        """Show or hide the music player sidebar."""

        visible = self.window.music_player_container.get_visible()
        self.window.music_player_container.set_visible(not visible)

    def destroy(self):

        self.file_list_view.destroy()
        self.__dict__.clear()
