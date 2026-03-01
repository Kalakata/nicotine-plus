# SPDX-FileCopyrightText: 2025 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import math
import os

from gi.repository import Gdk
from gi.repository import Gio
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.dialogs import EntryDialog
from pynicotine.gtkgui.widgets.dialogs import OptionDialog
from pynicotine.gtkgui.widgets.filechooser import FileChooser
from pynicotine.gtkgui.widgets.popupmenu import PopupMenu
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
            self.cutoff_toggle,
            self.duration_label,
            self.file_browser_box,
            self.file_list_container,
            self.header_box,
            self.next_button,
            self.now_playing_box,
            self.play_button,
            self.position_label,
            self.prev_button,
            self.quality_bar,
            self.quality_score_label,
            self.seek_box,
            self.spectrogram_area,
            self.spectrogram_box,
            self.stop_button,
            self.track_artist_label,
            self.track_title_label,
            self.verdict_label,
            self.volume_box,
            self.volume_scale,
            self.waveform_area
        ) = ui.load(scope=self, path="musicplayer.ui")

        self.window = window
        self._current_folder = None
        self._file_list = []      # ordered list of audio file paths in current folder
        self._current_index = -1  # index in _file_list of currently playing file
        self._seeking = False     # True while user is dragging the seek bar
        self._spectrogram = None  # numpy 2D array of spectrogram data
        self._analysis_result = None
        self._waveform_data = None       # list of 0.0-1.0 floats for waveform bars
        self._playback_progress = 0.0    # 0.0-1.0 playback position
        self._playback_duration = 0.0    # total duration in seconds
        self._current_playing_file = None  # track which file is loaded
        self._folder_monitor = None      # Gio.FileMonitor for current folder
        self._refresh_timer_id = None    # debounce timer for folder refresh
        self._known_files = set()        # files present at initial load
        self._new_files = set()          # files added during refreshes (starred)
        self._listened_files = set()     # new files that have been played (hollow star)
        self._is_refresh = False         # True when _load_folder is called from refresh
        self._show_cutoff_overlay = True # toggle for red cutoff zone on spectrogram
        self._quality_score = -1         # 0-100 quality percentage, -1 = not analyzed
        self._freq_zoom = 1.0            # frequency axis zoom level (1.0 = full range)
        self._freq_center = 0.5          # center of visible frequency range (0=low, 1=high)
        self._drag_start_center = 0.5    # saved center at drag start

        # Append our container into the mainwindow's music_player_container
        if GTK_API_VERSION >= 4:
            window.music_player_container.append(self.container)
        else:
            window.music_player_container.add(self.container)

        # File list TreeView
        self.file_list_view = TreeView(
            window, parent=self.file_list_container, name="music_player_files",
            activate_row_callback=self.on_file_activated,
            select_row_callback=self.on_file_selected,
            persistent_widths=True,
            columns={
                "icon": {
                    "column_type": "icon",
                    "title": "",
                    "width": 25,
                    "hide_header": True
                },
                "ext": {
                    "column_type": "text",
                    "title": "",
                    "width": 35,
                    "hide_header": True
                },
                "filename": {
                    "column_type": "text",
                    "title": _("Name"),
                    "width": 200,
                    "expand_column": True,
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
                "mtime_data": {
                    "data_type": GObject.TYPE_DOUBLE,
                    "default_sort_type": "descending"
                },
                "path_data": {
                    "data_type": GObject.TYPE_STRING
                }
            }
        )

        # Right-click context menu
        self.popup_menu = PopupMenu(
            window.application,
            parent=self.file_list_view.widget,
            callback=self._on_popup_menu
        )
        self.popup_menu.add_items(
            ("#" + _("_Open in File Manager"), self.on_open_in_folder),
            ("#" + _("Re_name"), self.on_rename_file),
            ("", None),
            ("#" + _("_Delete"), self.on_delete_file),
        )

        # Capture space key to toggle play/pause instead of activating tree rows
        if GTK_API_VERSION >= 4:
            key_controller = Gtk.EventControllerKey()
            key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            key_controller.connect("key-pressed", self._on_key_pressed)
            self.container.add_controller(key_controller)
        else:
            self.container.connect("key-press-event", self._on_key_pressed_gtk3)

        # Quality bar drawing
        if GTK_API_VERSION >= 4:
            self.quality_bar.set_draw_func(self._draw_quality_bar)
        else:
            self.quality_bar.connect("draw", self._draw_quality_bar_gtk3)

        # Spectrogram drawing + zoom/pan gestures
        if GTK_API_VERSION >= 4:
            self.spectrogram_area.set_draw_func(self._draw_spectrogram)

            scroll_controller = Gtk.EventControllerScroll()
            scroll_controller.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
            scroll_controller.connect("scroll", self._on_spectrogram_scroll)
            self.spectrogram_area.add_controller(scroll_controller)

            spec_drag = Gtk.GestureDrag()
            spec_drag.connect("drag-begin", self._on_spectrogram_drag_begin)
            spec_drag.connect("drag-update", self._on_spectrogram_drag_update)
            self.spectrogram_area.add_controller(spec_drag)

            # Double-click to reset zoom
            spec_click = Gtk.GestureClick()
            spec_click.set_button(0)
            spec_click.connect("pressed", self._on_spectrogram_click)
            self.spectrogram_area.add_controller(spec_click)
        else:
            self.spectrogram_area.connect("draw", self._draw_spectrogram_gtk3)
            self.spectrogram_area.add_events(
                Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.BUTTON_MOTION_MASK
            )
            self.spectrogram_area.connect("scroll-event", self._on_spectrogram_scroll_gtk3)
            self.spectrogram_area.connect("button-press-event", self._on_spectrogram_click_gtk3)
            self.spectrogram_area.connect("motion-notify-event", self._on_spectrogram_drag_gtk3)

        # Waveform drawing + seek gestures
        if GTK_API_VERSION >= 4:
            self.waveform_area.set_draw_func(self._draw_waveform)

            click_gesture = Gtk.GestureClick()
            click_gesture.connect("pressed", self._on_waveform_clicked)
            self.waveform_area.add_controller(click_gesture)

            drag_gesture = Gtk.GestureDrag()
            drag_gesture.connect("drag-update", self._on_waveform_dragged)
            self.waveform_area.add_controller(drag_gesture)
        else:
            self.waveform_area.connect("draw", self._draw_waveform_gtk3)
            self.waveform_area.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_MOTION_MASK
            )
            self.waveform_area.connect("button-press-event", self._on_waveform_clicked_gtk3)
            self.waveform_area.connect("motion-notify-event", self._on_waveform_dragged_gtk3)

        # Connect events
        for event_name, callback in (
            ("music-player-state-changed", self._on_state_changed),
            ("music-player-position-updated", self._on_position_updated),
            ("music-player-analysis-complete", self._on_analysis_complete),
            ("music-player-waveform-ready", self._on_waveform_ready),
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
        self._watch_folder(folder_path)

        try:
            entries = sorted(os.listdir(folder_path))
        except OSError:
            return

        current_files = set()

        for entry in entries:
            full_path = os.path.join(folder_path, entry)
            mtime = self._get_file_mtime(full_path)

            if os.path.isdir(full_path):
                self.file_list_view.add_row(
                    [
                        "folder-symbolic",    # icon
                        "",                    # ext
                        entry,                 # filename
                        "",                    # bitrate
                        "",                    # duration
                        mtime,                 # mtime_data
                        full_path              # path_data
                    ],
                    select_row=False
                )
                continue

            _name, ext = os.path.splitext(entry)
            if ext.lower() not in AUDIO_EXTENSIONS:
                continue

            current_files.add(full_path)

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

            # New files from refresh get a highlighted icon
            if self._is_refresh and full_path not in self._known_files:
                self._new_files.add(full_path)

            if full_path in self._new_files:
                icon = "non-starred-symbolic" if full_path in self._listened_files else "starred-symbolic"
            else:
                icon = "emblem-documents-symbolic"

            ext_label = ext.lstrip(".").upper()

            self._file_list.append(full_path)
            self.file_list_view.add_row(
                [
                    icon,                         # icon
                    ext_label,                    # ext
                    entry,                        # filename
                    bitrate_str,                  # bitrate
                    duration_str,                 # duration
                    mtime,                        # mtime_data
                    full_path                     # path_data
                ],
                select_row=False
            )

        if not self._is_refresh:
            # Folder change: reset known files and clear stars
            self._known_files = current_files
            self._new_files.clear()
            self._listened_files.clear()
        else:
            # Refresh: add new files to known set for future refreshes
            self._known_files.update(current_files)

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

    def _watch_folder(self, folder_path):
        """Set up a file monitor on the current folder."""

        # Cancel previous monitor
        if self._folder_monitor is not None:
            self._folder_monitor.cancel()
            self._folder_monitor = None

        try:
            gfile = Gio.File.new_for_path(folder_path)
            self._folder_monitor = gfile.monitor_directory(
                Gio.FileMonitorFlags.NONE, None
            )
            self._folder_monitor.connect("changed", self._on_folder_changed)
        except GLib.Error:
            self._folder_monitor = None

    def _on_folder_changed(self, _monitor, _file, _other_file, event_type):
        """Called when a file is added, removed, or changed in the watched folder."""

        if event_type not in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
        ):
            return

        # Debounce: wait 1 second after the last change before refreshing
        if self._refresh_timer_id is not None:
            GLib.source_remove(self._refresh_timer_id)

        self._refresh_timer_id = GLib.timeout_add_seconds(1, self._refresh_folder)

    def _refresh_folder(self):
        """Reload the current folder, preserving the currently playing file."""

        self._refresh_timer_id = None

        if self._current_folder and os.path.isdir(self._current_folder):
            playing_path = None
            if 0 <= self._current_index < len(self._file_list):
                playing_path = self._file_list[self._current_index]

            self._is_refresh = True
            self._load_folder(self._current_folder)
            self._is_refresh = False

            if playing_path and playing_path in self._file_list:
                self._current_index = self._file_list.index(playing_path)

        return GLib.SOURCE_REMOVE

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        """GTK 4: intercept space to toggle play/pause."""

        if keyval == Gdk.KEY_space:
            self.on_play_pause()
            return Gdk.EVENT_STOP

        return Gdk.EVENT_PROPAGATE

    def _on_key_pressed_gtk3(self, _widget, event):
        """GTK 3: intercept space to toggle play/pause."""

        if event.keyval == Gdk.KEY_space:
            self.on_play_pause()
            return True

        return False

    def on_file_selected(self, _treeview, iterator):
        """Single click/select: play audio files, ignore folders."""

        if iterator is None:
            return

        file_path = self.file_list_view.get_row_value(iterator, "path_data")
        if not file_path or os.path.isdir(file_path):
            return

        # Don't restart if this file is already loaded (handles pause/resume via space)
        if file_path == self._current_playing_file:
            return

        if core.musicplayer is not None:
            core.musicplayer.play(file_path)

            # Mark new file as listened and update icon to hollow star
            if file_path in self._new_files and file_path not in self._listened_files:
                self._listened_files.add(file_path)
                self.file_list_view.set_row_value(iterator, "icon", "non-starred-symbolic")

            try:
                self._current_index = self._file_list.index(file_path)
            except ValueError:
                self._current_index = -1

    def on_file_activated(self, _list_view, _iterator, _column_id):
        """Double click: navigate into folders."""

        iterator = self.file_list_view.get_selected_rows()
        if not iterator:
            return

        for row_iter in iterator:
            file_path = self.file_list_view.get_row_value(row_iter, "path_data")
            break

        if not file_path:
            return

        if os.path.isdir(file_path):
            self._load_folder(file_path)

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

    def on_cutoff_toggled(self, button):

        self._show_cutoff_overlay = button.get_active()
        self.spectrogram_area.queue_draw()

    def on_volume_changed(self, scale):

        value = scale.get_value() / 100.0

        if core.musicplayer is not None:
            # Set volume directly on the GStreamer element — no scheduling overhead
            core.musicplayer.set_volume(value)

        return False

    # Context Menu #

    def _get_selected_file_path(self):
        """Get the file path of the currently selected row."""

        iterators = self.file_list_view.get_selected_rows()
        if not iterators:
            return None

        for row_iter in iterators:
            return self.file_list_view.get_row_value(row_iter, "path_data")

        return None

    def _on_popup_menu(self, menu, _widget):
        """Called before the context menu is shown."""

        file_path = self._get_selected_file_path()
        has_selection = file_path is not None and os.path.isfile(file_path)

        for label in (_("_Open in File Manager"), _("Re_name"), _("_Delete")):
            if label in menu.actions:
                menu.actions[label].set_enabled(has_selection)

    def on_open_in_folder(self, *_args):

        file_path = self._get_selected_file_path()
        if not file_path:
            return

        folder = os.path.dirname(file_path) if os.path.isfile(file_path) else file_path

        if hasattr(Gio, "AppInfo"):
            try:
                Gio.AppInfo.launch_default_for_uri(
                    Gio.File.new_for_path(folder).get_uri(), None
                )
            except GLib.Error:
                pass

    def on_rename_file(self, *_args):

        file_path = self._get_selected_file_path()
        if not file_path or not os.path.isfile(file_path):
            return

        old_name = os.path.basename(file_path)

        EntryDialog(
            self.window.application,
            title=_("Rename File"),
            message=_("Enter new name:"),
            default=old_name,
            action_button_label=_("_Rename"),
            callback=self._on_rename_response,
            callback_data=file_path
        ).present()

    def _on_rename_response(self, dialog, _response_id, file_path):

        new_name = dialog.get_entry_value()
        if not new_name or new_name == os.path.basename(file_path):
            return

        new_path = os.path.join(os.path.dirname(file_path), new_name)

        try:
            os.rename(file_path, new_path)
        except OSError as error:
            log.add("Music player: failed to rename file: %s", error)
            return

        # Stop playback if renaming the currently playing file
        if file_path == self._current_playing_file:
            if core.musicplayer is not None:
                core.musicplayer.stop()
            self._current_playing_file = None

    def on_delete_file(self, *_args):

        file_path = self._get_selected_file_path()
        if not file_path or not os.path.isfile(file_path):
            return

        filename = os.path.basename(file_path)

        OptionDialog(
            self.window.application,
            title=_("Delete File"),
            message=_("Are you sure you want to permanently delete '%s'?") % filename,
            destructive_response_id="ok",
            callback=self._on_delete_response,
            callback_data=file_path
        ).present()

    def _on_delete_response(self, _dialog, _response_id, file_path):

        try:
            # Stop playback if deleting the currently playing file
            if file_path == self._current_playing_file:
                if core.musicplayer is not None:
                    core.musicplayer.stop()
                self._current_playing_file = None

            os.remove(file_path)
        except OSError as error:
            log.add("Music player: failed to delete file: %s", error)

    # Event Callbacks #

    def _on_state_changed(self, state, file_path):

        if state == "playing":
            self.play_button.set_icon_name("media-playback-pause-symbolic")

            # Only regenerate waveform/analysis for a new track, not on resume
            is_new_track = (file_path != self._current_playing_file)
            if is_new_track and file_path and core.musicplayer is not None:
                self._current_playing_file = file_path
                self._update_track_info(file_path)

                self._waveform_data = None
                self._playback_progress = 0.0
                self.waveform_area.queue_draw()

                self.verdict_label.set_text(_("Analyzing..."))
                self.quality_score_label.set_text("")
                self._quality_score = -1
                self.quality_bar.queue_draw()
                self._spectrogram = None
                self._analysis_result = None
                self._freq_zoom = 1.0
                self._freq_center = 0.5
                self.spectrogram_area.queue_draw()

                core.musicplayer.analyze_and_generate_waveform(file_path)

        elif state == "paused":
            self.play_button.set_icon_name("media-playback-start-symbolic")
        elif state == "stopped":
            self.play_button.set_icon_name("media-playback-start-symbolic")
            self.position_label.set_text("0:00")
            self.duration_label.set_text("0:00")
            self._playback_progress = 0.0
            self._current_playing_file = None
            self.waveform_area.queue_draw()

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

        self._playback_duration = duration
        self._playback_progress = current / max(duration, 0.001)
        self.waveform_area.queue_draw()

    def _on_analysis_complete(self, file_path, spectrogram, result):

        self._spectrogram = spectrogram
        self._analysis_result = result

        cutoff = result.get("cutoff_hz", 0)
        reported = result.get("reported_bitrate")
        estimated = result.get("estimated_source_bitrate")
        sample_rate = result.get("sample_rate", 44100)
        nyquist = sample_rate / 2.0

        # Compute quality score (0-100)
        self._quality_score = min(100, int(cutoff / nyquist * 100)) if nyquist > 0 else 0

        # One-word verdict + score for the score label
        if self._quality_score >= 85:
            verdict_word = _("Genuine")
        elif self._quality_score >= 65:
            verdict_word = _("Lossy")
        else:
            verdict_word = _("Poor")

        self.quality_score_label.set_text(f"{self._quality_score}%  {verdict_word}")
        self.quality_bar.queue_draw()

        # Technical detail line
        ext = os.path.splitext(file_path)[1].lstrip(".").upper() if file_path else ""
        bitrate_str = f"{reported} kbps" if reported else "? kbps"
        cutoff_khz = f"{cutoff / 1000:.1f}" if cutoff else "?"
        detail = f"{ext} {bitrate_str}  \u2192  cutoff {cutoff_khz} kHz"

        if estimated and self._quality_score < 85:
            detail += f"  (~{estimated} kbps source)"

        self.verdict_label.set_text(detail)
        self.spectrogram_area.queue_draw()

    def _on_waveform_ready(self, file_path, waveform_data):

        self._waveform_data = waveform_data
        self.waveform_area.queue_draw()

    # Waveform Seek Bar #

    def _draw_waveform_gtk3(self, widget, cr):
        """GTK 3 draw signal handler."""

        allocation = widget.get_allocation()
        self._render_waveform(cr, allocation.width, allocation.height)

    def _draw_waveform(self, area, cr, width, height):
        """GTK 4 draw function."""

        self._render_waveform(cr, width, height)

    def _render_waveform(self, cr, width, height):
        """Render SoundCloud-style waveform bars with progress coloring."""

        if not self._waveform_data:
            return

        num_bars = len(self._waveform_data)
        bar_gap = 1
        bar_width = max(1, (width - (num_bars - 1) * bar_gap) / num_bars)
        center_y = height / 2
        max_bar_height = height * 0.9

        progress_x = self._playback_progress * width

        for i, amplitude in enumerate(self._waveform_data):
            x = i * (bar_width + bar_gap)

            scaled = amplitude
            bar_height = max(2, scaled * max_bar_height)

            # Played = bright blue, unplayed = dim blue
            if x + bar_width <= progress_x:
                cr.set_source_rgb(0.25, 0.55, 1.0)
            elif x < progress_x:
                played_width = progress_x - x
                cr.set_source_rgb(0.25, 0.55, 1.0)
                cr.rectangle(x, center_y - bar_height / 2, played_width, bar_height)
                cr.fill()
                cr.set_source_rgb(0.2, 0.3, 0.45)
                cr.rectangle(x + played_width, center_y - bar_height / 2,
                             bar_width - played_width, bar_height)
                cr.fill()
                continue
            else:
                cr.set_source_rgb(0.2, 0.3, 0.45)

            cr.rectangle(x, center_y - bar_height / 2, bar_width, bar_height)
            cr.fill()

    def _seek_from_waveform_x(self, x, width):
        """Seek to position based on x coordinate within waveform area."""

        if core.musicplayer is None or self._playback_duration <= 0:
            return

        ratio = max(0.0, min(1.0, x / max(width, 1)))
        target = ratio * self._playback_duration
        core.musicplayer.seek(target)

    def _on_waveform_clicked(self, gesture, _n_press, x, _y):
        """GTK 4: click on waveform to seek."""

        width = self.waveform_area.get_width()
        self._seek_from_waveform_x(x, width)

    def _on_waveform_dragged(self, gesture, offset_x, _offset_y):
        """GTK 4: drag on waveform to scrub."""

        success, start_x, _ = gesture.get_start_point()
        if not success:
            return

        x = start_x + offset_x
        width = self.waveform_area.get_width()
        self._seek_from_waveform_x(x, width)

    def _on_waveform_clicked_gtk3(self, widget, event):
        """GTK 3: click on waveform to seek."""

        width = widget.get_allocation().width
        self._seek_from_waveform_x(event.x, width)
        return True

    def _on_waveform_dragged_gtk3(self, widget, event):
        """GTK 3: drag on waveform to scrub."""

        if event.state & Gdk.ModifierType.BUTTON1_MASK:
            width = widget.get_allocation().width
            self._seek_from_waveform_x(event.x, width)
        return True

    # Quality Score Bar #

    def _draw_quality_bar_gtk3(self, widget, cr):
        allocation = widget.get_allocation()
        self._render_quality_bar(cr, allocation.width, allocation.height)

    def _draw_quality_bar(self, area, cr, width, height):
        self._render_quality_bar(cr, width, height)

    def _render_quality_bar(self, cr, width, height):
        """Render the quality score bar with color gradient."""

        radius = height / 2

        # Background (unfilled portion)
        cr.set_source_rgb(0.2, 0.2, 0.25)
        self._rounded_rect(cr, 0, 0, width, height, radius)
        cr.fill()

        if self._quality_score < 0:
            return

        score = max(0, min(100, self._quality_score))
        fill_width = max(height, score / 100.0 * width)

        # Color based on score
        if score >= 85:
            r, g, b = 0.30, 0.69, 0.31   # green #4CAF50
        elif score >= 65:
            r, g, b = 1.00, 0.60, 0.00   # orange #FF9800
        else:
            r, g, b = 0.96, 0.26, 0.21   # red #F44336

        # Filled portion
        cr.set_source_rgb(r, g, b)
        self._rounded_rect(cr, 0, 0, fill_width, height, radius)
        cr.fill()

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        """Draw a rounded rectangle path."""

        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    # Spectrogram Zoom/Pan #

    def _apply_zoom(self, delta, mouse_y=None, height=None):
        """Zoom the frequency axis. delta > 0 zooms in, delta < 0 zooms out."""

        old_zoom = self._freq_zoom
        factor = 1.15
        if delta > 0:
            self._freq_zoom = min(self._freq_zoom * factor, 20.0)
        else:
            self._freq_zoom = max(self._freq_zoom / factor, 1.0)

        # Zoom toward mouse position
        if mouse_y is not None and height and height > 0 and old_zoom != self._freq_zoom:
            mouse_ratio = mouse_y / height
            visible_half = 0.5 / self._freq_zoom
            self._freq_center = 1.0 - mouse_ratio
            self._freq_center = max(visible_half, min(1.0 - visible_half, self._freq_center))

        # Clamp center so visible range stays within 0-1
        visible_half = 0.5 / self._freq_zoom
        self._freq_center = max(visible_half, min(1.0 - visible_half, self._freq_center))

        self.spectrogram_area.queue_draw()

    def _on_spectrogram_scroll(self, _controller, _dx, dy):
        """GTK 4: scroll to zoom frequency axis."""

        height = self.spectrogram_area.get_height()
        self._apply_zoom(-dy, height=height)
        return Gdk.EVENT_STOP

    def _on_spectrogram_scroll_gtk3(self, widget, event):
        """GTK 3: scroll to zoom frequency axis."""

        if event.direction == Gdk.ScrollDirection.UP:
            self._apply_zoom(1, event.y, widget.get_allocation().height)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._apply_zoom(-1, event.y, widget.get_allocation().height)
        return True

    def _on_spectrogram_drag_begin(self, gesture, _x, _y):
        """GTK 4: save center at drag start."""

        self._drag_start_center = self._freq_center

    def _on_spectrogram_drag_update(self, gesture, _offset_x, offset_y):
        """GTK 4: drag to pan frequency axis."""

        height = self.spectrogram_area.get_height()
        if height <= 0 or self._freq_zoom <= 1.0:
            return

        pan_ratio = offset_y / height / self._freq_zoom
        self._freq_center = self._drag_start_center + pan_ratio

        visible_half = 0.5 / self._freq_zoom
        self._freq_center = max(visible_half, min(1.0 - visible_half, self._freq_center))
        self.spectrogram_area.queue_draw()

    def _on_spectrogram_click(self, gesture, n_press, _x, _y):
        """GTK 4: double-click to reset zoom."""

        if n_press == 2:
            self._freq_zoom = 1.0
            self._freq_center = 0.5
            self.spectrogram_area.queue_draw()

    def _on_spectrogram_click_gtk3(self, widget, event):
        """GTK 3: double-click to reset zoom."""

        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self._freq_zoom = 1.0
            self._freq_center = 0.5
            self.spectrogram_area.queue_draw()
        return True

    def _on_spectrogram_drag_gtk3(self, widget, event):
        """GTK 3: drag to pan frequency axis."""

        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return True

        height = widget.get_allocation().height
        if height <= 0 or self._freq_zoom <= 1.0:
            return True

        # Simple pan based on motion delta
        pan_ratio = 2.0 / height / self._freq_zoom
        self._freq_center = max(0.0, min(1.0, self._freq_center + pan_ratio))

        visible_half = 0.5 / self._freq_zoom
        self._freq_center = max(visible_half, min(1.0 - visible_half, self._freq_center))
        self.spectrogram_area.queue_draw()
        return True

    # Spectrogram Drawing #

    def _draw_spectrogram_gtk3(self, widget, cr):
        """GTK 3 draw signal handler."""

        allocation = widget.get_allocation()
        self._render_spectrogram(cr, allocation.width, allocation.height)

    def _draw_spectrogram(self, area, cr, width, height):
        """GTK 4 draw function."""

        self._render_spectrogram(cr, width, height)

    def _freq_to_y(self, freq_hz, nyquist, plot_height):
        """Convert frequency to Y coordinate using linear scale with zoom/pan."""

        # Linear mapping: ratio = freq / nyquist
        full_ratio = min(freq_hz, nyquist) / nyquist

        # Apply zoom/pan
        visible_half = 0.5 / self._freq_zoom
        view_lo = self._freq_center - visible_half
        view_hi = self._freq_center + visible_half

        if view_hi == view_lo:
            return plot_height
        visible_ratio = (full_ratio - view_lo) / (view_hi - view_lo)

        return plot_height - (visible_ratio * plot_height)

    def _render_spectrogram(self, cr, width, height):
        """Render spectrogram with linear frequency scale (Spek-style)."""

        # Background
        cr.set_source_rgb(0.0, 0.0, 0.0)
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

        # Spek uses -120 to 0 dB range for full dynamic range visibility
        urange = 0.0
        lrange = -120.0
        db_range = urange - lrange

        label_width = 35
        plot_width = width - label_width
        plot_height = height - 2

        if plot_width <= 0 or plot_height <= 0:
            return

        sample_rate = 44100
        if self._analysis_result:
            sample_rate = self._analysis_result.get("sample_rate", 44100)
        nyquist = sample_rate / 2.0
        freq_per_band = nyquist / num_bands

        # Visible frequency window based on zoom/pan
        visible_half = 0.5 / self._freq_zoom
        view_lo = self._freq_center - visible_half
        view_hi = self._freq_center + visible_half

        # Draw spectrogram pixels with linear frequency mapping
        num_cols = min(num_frames, int(plot_width))
        x_step = max(1, plot_width / num_cols)
        num_y_pixels = int(plot_height)

        for col in range(num_cols):
            frame_idx = int(col * num_frames / num_cols)
            if frame_idx >= num_frames:
                break

            x = col * x_step

            for py in range(num_y_pixels):
                visible_ratio = 1.0 - (py / plot_height)
                full_ratio = view_lo + visible_ratio * (view_hi - view_lo)

                if full_ratio < 0.0 or full_ratio > 1.0:
                    continue

                band = min(int(full_ratio * num_bands), num_bands - 1)

                # Clamp to dB range and normalize to 0-1 (Spek approach)
                value = max(lrange, min(urange, spectrogram[frame_idx, band]))
                level = (value - lrange) / db_range

                r, g, b = self._spectrogram_color(level)
                cr.set_source_rgb(r, g, b)
                cr.rectangle(x, py, math.ceil(x_step), 1)
                cr.fill()

        # Bitrate reference lines (dashed)
        cr.set_line_width(0.5)
        cr.set_font_size(7)
        for bitrate, cutoff_freq in ((64, 10000), (128, 16000), (192, 19000), (320, 20500)):
            if cutoff_freq >= nyquist:
                continue
            y = self._freq_to_y(cutoff_freq, nyquist, plot_height)
            if y < -10 or y > plot_height + 10:
                continue
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.5)
            cr.set_dash([3, 3])
            cr.move_to(0, y)
            cr.line_to(plot_width, y)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(0.6, 0.6, 0.6, 0.6)
            cr.move_to(plot_width + 2, y + 3)
            cr.show_text(f"{bitrate}")

        # Cutoff shading + line (togglable)
        if self._show_cutoff_overlay and self._analysis_result:
            cutoff_hz = self._analysis_result.get("cutoff_hz", 0)

            if 0 < cutoff_hz < nyquist:
                cutoff_y = self._freq_to_y(cutoff_hz, nyquist, plot_height)

                # Semi-transparent red overlay above cutoff
                cr.set_source_rgba(0.8, 0.1, 0.1, 0.2)
                cr.rectangle(0, 0, plot_width, cutoff_y)
                cr.fill()

                # Cutoff line
                cr.set_source_rgba(1.0, 0.2, 0.2, 0.9)
                cr.set_line_width(2)
                cr.move_to(0, cutoff_y)
                cr.line_to(plot_width, cutoff_y)
                cr.stroke()

                # Label
                cr.set_font_size(9)
                cr.set_source_rgba(1.0, 0.3, 0.3, 1.0)
                label = f"{cutoff_hz} Hz"
                cr.move_to(3, cutoff_y - 4)
                cr.show_text(label)

        # Frequency axis labels (dynamic based on sample rate and zoom)
        cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.set_font_size(8)

        freq_labels_khz = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20]
        if nyquist > 22050:
            freq_labels_khz.extend([30, 40])
        if nyquist > 48000:
            freq_labels_khz.extend([60, 80])

        for freq_khz in freq_labels_khz:
            freq_hz = freq_khz * 1000
            if freq_hz > nyquist:
                break
            y = self._freq_to_y(freq_hz, nyquist, plot_height)
            if y < -10 or y > plot_height + 10:
                continue
            cr.move_to(plot_width + 2, y + 3)
            if freq_khz >= 1:
                cr.show_text(f"{int(freq_khz)}k")
            else:
                cr.show_text(f"{int(freq_khz * 1000)}")

    @staticmethod
    def _spectrogram_color(level):
        """Map a 0-1 level to RGB using the SoX palette (same as Spek default).

        Based on Spek's spek-palette.cc sox palette implementation.
        """

        # Red channel
        if level >= 0.73:
            r = 1.0
        elif level >= 0.13:
            r = math.sin((level - 0.13) / 0.60 * math.pi / 2.0)
        else:
            r = 0.0

        # Green channel
        if level >= 0.91:
            g = 1.0
        elif level >= 0.60:
            g = math.sin((level - 0.60) / 0.31 * math.pi / 2.0)
        else:
            g = 0.0

        # Blue channel
        if level >= 0.78:
            b = (level - 0.78) / 0.22
        elif level < 0.60:
            b = 0.5 * math.sin(level / 0.60 * math.pi)
        else:
            b = 0.0

        return (r, g, b)

    # Utilities #

    @staticmethod
    def _get_file_mtime(file_path):
        """Get file modification time as a float (epoch seconds)."""

        try:
            return os.path.getmtime(file_path)
        except OSError:
            return 0.0

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

        if self._folder_monitor is not None:
            self._folder_monitor.cancel()
            self._folder_monitor = None

        if self._refresh_timer_id is not None:
            GLib.source_remove(self._refresh_timer_id)
            self._refresh_timer_id = None

        self.popup_menu.destroy()
        self.file_list_view.destroy()
        self.__dict__.clear()
