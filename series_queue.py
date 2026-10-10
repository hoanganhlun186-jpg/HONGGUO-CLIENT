"""Independent series tabs and a sequential full-pipeline queue for BOOM Studio."""
import os
import time
from PyQt6.QtWidgets import (QFileDialog, QTextEdit, QListView, QTreeView,
    QAbstractItemView)
from series_session import save_session, read_session, restore_controls
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QPushButton, QLabel, QInputDialog, QMessageBox)


def managed_host(parent):
    host = getattr(parent, 'host', parent)
    return host if getattr(host, '_batch_managed', False) else None


class QueueMessageBox(QMessageBox):
    """Keep normal dialogs for manual work; log managed-run notices instead."""
    @staticmethod
    def _notice(kind, parent, title, text, *args, **kwargs):
        host = managed_host(parent)
        if host is None:
            return getattr(QMessageBox, kind)(parent, title, text, *args, **kwargs)
        host._log(f'[{title}] {text}')
        if kind == 'warning':
            host._batch_last_warning = f'{title}: {text}'
        if kind in ('critical', 'question'):
            host._batch_complete(False, f'{title}: {text}')
        return QMessageBox.StandardButton.No if kind == 'question' else QMessageBox.StandardButton.Ok

    @staticmethod
    def information(parent, title, text, *args, **kwargs):
        return QueueMessageBox._notice('information', parent, title, text, *args, **kwargs)

    @staticmethod
    def warning(parent, title, text, *args, **kwargs):
        return QueueMessageBox._notice('warning', parent, title, text, *args, **kwargs)

    @staticmethod
    def critical(parent, title, text, *args, **kwargs):
        return QueueMessageBox._notice('critical', parent, title, text, *args, **kwargs)

    @staticmethod
    def question(parent, title, text, *args, **kwargs):
        return QueueMessageBox._notice('question', parent, title, text, *args, **kwargs)


def live_workers(page):
    """Include retained workers: custom done signals may precede QThread.finished."""
    candidates = []
    for owner in (page, getattr(page, 'dub_feature_tab', None)):
        if owner is None:
            continue
        for value in vars(owner).values():
            if isinstance(value, dict):
                candidates.extend(value.values())
            elif isinstance(value, (list, tuple)):
                candidates.extend(value)
            else:
                candidates.append(value)
    for worker in candidates:
        if callable(getattr(worker, 'isRunning', None)):
            try:
                if worker.isRunning():
                    return True
            except RuntimeError:
                pass
    return False


def preflight(page):
    if getattr(page, '_bulk_loading', False):
        return 'Thư mục vẫn đang nạp video, hãy đợi nạp xong.'
    if not getattr(page, 'dub_feature_tab', None):
        return 'Không nạp được module Sub → Dịch → Lồng.'
    cards = page._selected_cards()
    if not cards:
        return 'Chưa tick chọn tập nào.'
    missing_incomplete = []
    for card in cards:
        if os.path.isfile(card.video_path):
            continue
        try:
            completed = bool(page._card_state(card).get('done'))
        except Exception:
            completed = False
        if not completed:
            missing_incomplete.append(os.path.basename(card.video_path))
    if missing_incomplete:
        sample = ', '.join(missing_incomplete[:3])
        more = f' và {len(missing_incomplete)-3} tập khác' if len(missing_incomplete) > 3 else ''
        return f'Tập chưa hoàn tất bị thiếu video nguồn: {sample}{more}.'
    # One output folder per series prevents misleading grouped names/output collisions.
    directories = {os.path.normcase(os.path.abspath(os.path.dirname(c.video_path))) for c in cards}
    if len(directories) != 1:
        return 'Mỗi tab bộ phim cần dùng video trong một thư mục riêng.'
    if page.chk_intro.isChecked() and not os.path.isfile(page.intro_input.text().strip()):
        return 'Đã bật Cover nhưng chưa chọn ảnh bìa hợp lệ.'
    if page.cmb_merge_mode.currentText() == 'Theo phần' and page.chk_part_thumbnail.isChecked():
        if not page.chk_intro.isChecked() or not os.path.isfile(page.intro_input.text().strip()):
            return 'Thumbnail theo phần cần chọn ảnh Cover và bật Nhúng Ảnh Bìa.'
    return None


def make_multi_series_widget(series_class):
    class MultiSeriesRenderWidget(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._pages = []
            self._pending = []
            self._active_series = None
            self._queue_running = False
            self._stop_after_current = False
            self._idle_since = None
            self._results = []
            self._session_path = None
            self._session_loading = False
            self.setStyleSheet("""
                QPushButton#SeriesAdd { background:#087EAA; color:#FFFFFF;
                    border:1px solid #55D7FF; border-radius:6px; padding:8px 14px; font-weight:700; }
                QPushButton#SeriesRun { background:#176AE6; color:#FFFFFF;
                    border:1px solid #7CB5FF; border-radius:6px; padding:8px 14px; font-weight:700; }
                QPushButton#SeriesPause { background:#A95808; color:#FFFFFF;
                    border:1px solid #FFC46A; border-radius:6px; padding:8px 14px; font-weight:700; }
                QPushButton#SeriesAdd:hover { background:#119BCB; }
                QPushButton#SeriesRun:hover { background:#3285FA; }
                QPushButton#SeriesPause:hover { background:#CB7214; }
                QPushButton#SeriesAdd:disabled, QPushButton#SeriesRun:disabled,
                QPushButton#SeriesPause:disabled { background:#34465E; color:#C0CDDE; border-color:#63758D; }
                QLabel#SeriesStatus { background:#23354C; color:#EDF5FF;
                    padding:6px 10px; border:1px solid #496583; border-radius:5px; }
                QTabWidget#SeriesTabs::pane { border:1px solid #526F91; }
                QTabWidget#SeriesTabs > QTabBar::tab { background:#344B67; color:#F1F6FF;
                    border:1px solid #6683A6; padding:9px 14px; margin-right:3px; font-weight:700; }
                QTabWidget#SeriesTabs > QTabBar::tab:selected { background:#137ADF; color:#FFFFFF;
                    border:1px solid #76D7FF; }
                QTabWidget#SeriesTabs > QTabBar::tab:hover { background:#426B96; }
            """)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            row = QHBoxLayout()
            self.btn_add_series = QPushButton('+ Thêm bộ')
            self.btn_add_folders = QPushButton('📁 Chọn nhiều thư mục')
            self.btn_run_series = QPushButton('▶ CHẠY TẤT CẢ BỘ · Tách → Dịch → Lồng → Xuất')
            self.btn_pause_series = QPushButton('Dừng sau bộ này')
            self.btn_add_series.setObjectName('SeriesAdd')
            self.btn_add_folders.setObjectName('SeriesAdd')
            self.btn_run_series.setObjectName('SeriesRun')
            self.btn_pause_series.setObjectName('SeriesPause')
            for button in (self.btn_add_series, self.btn_add_folders,
                           self.btn_run_series, self.btn_pause_series):
                button.setMinimumHeight(36)
            self.btn_pause_series.setEnabled(False)
            row.addWidget(self.btn_add_series)
            row.addWidget(self.btn_add_folders)
            row.addWidget(self.btn_run_series, 1)
            row.addWidget(self.btn_pause_series)
            layout.addLayout(row)
            session_row = QHBoxLayout()
            self.btn_save_session = QPushButton("Lưu phiên")
            self.btn_open_session = QPushButton("Mở phiên")
            self.btn_resume_series = QPushButton("Tiếp tục bộ chờ / thử lại bộ lỗi")
            self.btn_details = QPushButton("Chi tiết / Nhật ký")
            for b in (self.btn_save_session, self.btn_open_session, self.btn_resume_series, self.btn_details):
                b.setStyleSheet("background:#345574;color:white;padding:6px;border:1px solid #7093B8;border-radius:4px;")
                session_row.addWidget(b)
            layout.addLayout(session_row)
            self.queue_details = QTextEdit()
            self.queue_details.setReadOnly(True)
            self.queue_details.document().setMaximumBlockCount(1500)
            self.queue_details.setMaximumHeight(180)
            self.queue_details.hide()
            layout.addWidget(self.queue_details)
            self.btn_details.clicked.connect(lambda: self.queue_details.setVisible(not self.queue_details.isVisible()))
            self.btn_save_session.clicked.connect(self._save_session_dialog)
            self.btn_open_session.clicked.connect(self._open_session_dialog)
            self.btn_resume_series.clicked.connect(lambda: self._start_series_queue(resume=True))
            self.queue_status = QLabel('Mỗi bộ một tab: thêm video, chọn giọng, thiết kế và Cover riêng. Bấm đúp tên tab để đổi tên.')
            self.queue_status.setObjectName('SeriesStatus')
            self.queue_status.setWordWrap(True)
            layout.addWidget(self.queue_status)
            self.series_tabs = QTabWidget()
            self.series_tabs.setObjectName('SeriesTabs')
            self.series_tabs.setTabsClosable(True)
            self.series_tabs.setMovable(False)
            layout.addWidget(self.series_tabs, 1)
            self.btn_add_series.clicked.connect(self._add_series)
            self.btn_add_folders.clicked.connect(self._pick_series_folders)
            self.btn_run_series.clicked.connect(self._start_series_queue)
            self.btn_pause_series.clicked.connect(self._stop_queue_after_current)
            self.series_tabs.tabCloseRequested.connect(self._close_series)
            self.series_tabs.tabBarDoubleClicked.connect(self._rename_series)
            self._queue_timer = QTimer(self)
            self._queue_timer.setInterval(500)
            self._queue_timer.timeout.connect(self._poll_queue)
            self._add_series()

        def __getattr__(self, name):
            # Existing app integrations adding videos continue targeting the visible tab.
            pages = self.__dict__.get('_pages', [])
            tabs = self.__dict__.get('series_tabs')
            if pages and tabs is not None:
                page = tabs.currentWidget()
                if page is not None:
                    return getattr(page, name)
            raise AttributeError(name)

        def _add_series(self):
            if self._queue_running:
                return
            return self._create_series()

        def _create_series(self, name=None, make_current=True):
            """Create one independent series page and return it."""
            page = series_class(self)
            page._series_name = str(name or f'Bộ {len(self._pages) + 1}')
            page._batch_managed = False
            page._batch_result = None
            page._queue_state = 'waiting'
            old_log = page._log
            def log_with_series(message, p=page, original=old_log):
                original(message)
                self.queue_details.append(f"[{p._series_name}] {message}")
            page._log = log_with_series
            page._queue_log_sink = lambda message, p=page: self.queue_details.append(f"[{p._series_name}] {message}")
            self._pages.append(page)
            index = self.series_tabs.addTab(page, page._series_name)
            if make_current:
                self.series_tabs.setCurrentIndex(index)
            return page

        def _pick_series_folders(self):
            """Select several folders; each valid folder becomes one series tab."""
            if self._queue_running or self._session_loading:
                return
            if any(getattr(p, '_bulk_loading', False) for p in self._pages):
                self.queue_status.setText('Hãy chờ các thư mục hiện tại nạp xong rồi chọn thêm.')
                return

            dialog = QFileDialog(self, 'Chọn nhiều thư mục — mỗi thư mục là một Bộ')
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
            dialog.setLabelText(QFileDialog.DialogLabel.Accept, 'Thêm các thư mục đã chọn')
            dialog.setLabelText(QFileDialog.DialogLabel.FileName, 'Các thư mục:')
            for view_type in (QListView, QTreeView):
                for view in dialog.findChildren(view_type):
                    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            if not dialog.exec():
                return

            folders = []
            seen = set()
            for folder in dialog.selectedFiles():
                if not os.path.isdir(folder):
                    continue
                absolute = os.path.abspath(folder)
                key = os.path.normcase(absolute)
                if key not in seen:
                    seen.add(key)
                    folders.append(absolute)
            if not folders:
                QMessageBox.information(self, 'Chưa chọn thư mục',
                    'Hãy giữ Ctrl hoặc Shift để chọn nhiều thư mục.')
                return

            prepared = []
            skipped = []
            scanner = self.series_tabs.currentWidget() or (self._pages[0] if self._pages else None)
            if scanner is None:
                scanner = self._create_series(make_current=False)
            for folder in folders:
                pairs = scanner._auto_pair(folder)
                if pairs:
                    prepared.append((folder, pairs))
                else:
                    skipped.append(os.path.basename(folder) or folder)
            if not prepared:
                QMessageBox.information(self, 'Không thấy video',
                    'Các thư mục đã chọn không có video hỗ trợ (.mp4, .mkv, .mov, .avi).')
                return

            reusable = self.series_tabs.currentWidget()
            if reusable is not None and (getattr(reusable, 'cards', None) or
                                         getattr(reusable, '_bulk_loading', False)):
                reusable = None
            made = []
            for folder, pairs in prepared:
                name = os.path.basename(os.path.normpath(folder)) or f'Bộ {len(self._pages) + 1}'
                if reusable is not None:
                    page, reusable = reusable, None
                    page._series_name = name
                    self.series_tabs.setTabText(self.series_tabs.indexOf(page), name)
                else:
                    page = self._create_series(name=name, make_current=False)
                page._source_folder = folder
                page._start_bulk_card_import(pairs, name)
                made.append(page)
            self.series_tabs.setCurrentWidget(made[0])
            message = f'Đã tách {len(made)} thư mục thành {len(made)} Bộ riêng; đang nạp video.'
            if skipped:
                message += f' Bỏ qua {len(skipped)} thư mục không có video.'
                self.queue_details.append('Bỏ qua không có video: ' + ', '.join(skipped))
            self.queue_status.setText(message)

        def _rename_series(self, index):
            if self._queue_running or index < 0:
                return
            page = self.series_tabs.widget(index)
            name, ok = QInputDialog.getText(self, 'Tên bộ phim', 'Tên bộ:', text=page._series_name)
            if ok and name.strip():
                page._series_name = name.strip()
                self.series_tabs.setTabText(index, name.strip())

        def _close_series(self, index):
            page = self.series_tabs.widget(index)
            if page is None or self._queue_running or live_workers(page) or getattr(page, '_bulk_loading', False):
                self.queue_status.setText('Hãy chờ xử lý xong trước khi đóng tab.')
                return
            if len(self._pages) == 1:
                return
            page.media_player.stop()
            self.series_tabs.removeTab(index)
            self._pages.remove(page)
            page.deleteLater()

        def _start_series_queue(self, resume=False):
            if self._queue_running or self._session_loading:
                return
            if any(live_workers(p) or getattr(p, '_render_running', False) or getattr(p, '_bulk_loading', False) for p in self._pages):
                self.queue_status.setText('Đang có tác vụ thủ công/nạp video. Hãy chờ xong rồi chạy tất cả bộ.')
                return
            pages = [p for p in self._pages if p.cards and (not resume or getattr(p, "_queue_state", "waiting") in ("waiting", "failed"))]
            if not pages:
                self.queue_status.setText('Không có bộ đang chờ.' if resume else 'Chưa có bộ phim. Thêm video vào từng tab trước.')
                return
            # Validate all jobs before dispatch; never mix two tabs writing the same folder.
            occupied = set()
            self._pending = []
            self._results = []
            for page in pages:
                error = preflight(page)
                dirs = {os.path.normcase(os.path.abspath(os.path.dirname(c.video_path))) for c in page._selected_cards()}
                if not error and occupied.intersection(dirs):
                    error = 'Trùng thư mục xuất với bộ khác; hãy tách mỗi bộ vào thư mục riêng.'
                if error:
                    self._record(page, False, error)
                    continue
                occupied.update(dirs)
                if page.selected_card is not None:
                    page._save_design_to_card(page.selected_card)
                page._batch_cards = list(page._selected_cards())
                page._queue_state = "waiting"
                self._pending.append(page)
                self.series_tabs.setTabText(self.series_tabs.indexOf(page), page._series_name + ' · Chờ')
            if not self._pending:
                self._finish_queue()
                return
            self._autosave_session()
            self._queue_running = True
            self._stop_after_current = False
            self.btn_add_series.setEnabled(False)
            self.btn_add_folders.setEnabled(False)
            self.btn_run_series.setEnabled(False)
            self.btn_pause_series.setEnabled(True)
            self.btn_open_session.setEnabled(False)
            self.btn_resume_series.setEnabled(False)
            # Freeze each series' inputs and settings for this run; tabs remain viewable.
            for page in self._pages:
                page.setEnabled(False)
            self._queue_timer.start()
            self._dispatch_next()

        def _dispatch_next(self):
            if self._stop_after_current or not self._pending:
                self._finish_queue()
                return
            page = self._pending.pop(0)
            self._active_series = page
            self._idle_since = None
            page._batch_result = None
            page._batch_last_warning = ''
            page._batch_idle_retries = 0
            page._batch_managed = True
            # Mỗi bộ giữ lựa chọn riêng: chỉ bộ được tick mới đi luồng 3 file.
            page._separate_export_mode = bool(
                hasattr(page, 'chk_export_separate') and page.chk_export_separate.isChecked())
            page._separate_audio_started = False
            page._separate_audio_finished = False
            page._separate_audio_ok = False
            page._separate_audio_outputs = []
            page._separate_video_merge_result = None
            page._queue_state = "running"
            self._autosave_session()
            self.series_tabs.setCurrentWidget(page)
            self.series_tabs.setTabText(self.series_tabs.indexOf(page), page._series_name + ' · Đang chạy')
            self.queue_status.setText(f'Đang chạy {page._series_name}; còn {len(self._pending)} bộ chờ. Nếu lỗi, hàng đợi sẽ dừng tại đúng bộ này.')
            try:
                # Nếu từng tập đã đủ bộ 3 file nhưng chưa có file trọn bộ (ví
                # dụ app bị đóng ngay trước bước gộp), video nguồn có thể đã
                # được dọn. Không quay lại pipeline dịch/lồng; gộp trực tiếp từ
                # các đầu ra tập lẻ đang còn trên đĩa.
                states = [page._card_state(card) for card in page._selected_cards()]
                ready_to_merge = bool(
                    page._separate_export_mode
                    and states
                    and all(state.get('done') for state in states)
                    and page.cmb_merge_mode.currentText() == 'Gộp trọn bộ')
                if ready_to_merge:
                    audio_outputs = []
                    for card in page._selected_cards():
                        stem, _ext = os.path.splitext(card.video_path)
                        low = stem.lower()
                        for suffix in ('_dubbed', '_final', '_video_sub'):
                            if low.endswith(suffix):
                                stem = stem[:-len(suffix)]
                                break
                        audio_outputs.extend([
                            stem + '_audio_long_tieng.m4a',
                            stem + '_audio_goc.m4a'])
                    page._separate_audio_outputs = audio_outputs
                    page._separate_audio_ok = all(
                        os.path.isfile(path) and os.path.getsize(path) > 1024
                        for path in audio_outputs)
                    page._separate_audio_finished = page._separate_audio_ok
                    page._separate_audio_sealed = True
                    page._log('↻ Các tập đã đủ 3 file → bỏ qua dịch/lồng và gộp trọn bộ trực tiếp.')
                    page._start_render_all(streaming=False)
                else:
                    page.dub_feature_tab._run_full_pipeline()
            except Exception as exc:
                page._batch_complete(False, f'Không khởi động được: {exc}')

        def _poll_queue(self):
            page = self._active_series
            if page is None:
                return
            # Wait for the real finished event, including thumbnail embedding/cleanup.
            if live_workers(page):
                self._idle_since = None
                return
            result = page._batch_result
            if result is None:
                now = time.monotonic()
                if self._idle_since is None:
                    self._idle_since = now
                elif now - self._idle_since >= 15:
                    if getattr(page, '_batch_idle_retries', 0) < 1:
                        page._batch_idle_retries = 1
                        page._log('↻ Kiểm tra file hiện tại và thử chạy lại bộ một lần trước khi kết luận lỗi.')
                        pipeline = page.dub_feature_tab
                        pipeline._chain_after_stt = None
                        pipeline._chain_dub_after_translate = False
                        pipeline._render_after_dub = False
                        pipeline._dub_queue = []
                        pipeline._dub_running = False
                        pipeline._stop_card_poll()
                        page._render_running = False
                        page._render_queue = []
                        page._live_accepting = False
                        page._live_render = False
                        page.total_progress_end()
                        self._idle_since = None
                        try:
                            pipeline._run_full_pipeline()
                        except Exception as exc:
                            page._batch_complete(False, f'Chạy lại thất bại: {exc}')
                    else:
                        detail = getattr(page, '_batch_last_warning', '')
                        page._batch_complete(False, 'Quy trình vẫn dừng sau khi kiểm tra và chạy lại. ' +
                                             (detail or 'Kiểm tra nhật ký của bộ này.'))
                return
            pipeline = page.dub_feature_tab
            pipeline._chain_after_stt = None
            pipeline._chain_dub_after_translate = False
            pipeline._render_after_dub = False
            pipeline._dub_queue = []
            pipeline._dub_running = False
            pipeline._total_on = False
            if not result[0]:
                page._render_running = False
                page._render_queue = []
                page._merge_parts_queue = []
                page._merge_parts_active = False
            pipeline._stop_card_poll()
            page.total_progress_end()
            if result[0]:
                release = getattr(page, 'release_completed_series_files', None)
                if callable(release):
                    try:
                        release()
                    except Exception as exc:
                        page._log(f'⚠️ Không nhả hết file sau khi hoàn tất: {exc}')
            page._batch_managed = False
            page._batch_cards = None
            self._record(page, *result)
            self._active_series = None
            if not result[0]:
                # Chế độ hàng đợi nghiêm ngặt: một bộ chưa hoàn tất gói đầu ra
                # thì tuyệt đối không được nhảy sang bộ kế tiếp. Giữ nguyên
                # _pending để nút Tiếp tục sẽ quay lại bộ lỗi trước, sau đó mới
                # lần lượt chạy các bộ còn chờ.
                self._queue_timer.stop()
                self._queue_running = False
                self.btn_add_series.setEnabled(True)
                self.btn_add_folders.setEnabled(True)
                self.btn_open_session.setEnabled(True)
                self.btn_resume_series.setEnabled(True)
                self.btn_run_series.setEnabled(True)
                self.btn_pause_series.setEnabled(False)
                for queued_page in self._pages:
                    queued_page.setEnabled(True)
                self.queue_status.setText(
                    f'ĐÃ DỪNG tại {page._series_name}: {result[1]} · '
                    'Sửa nguyên nhân rồi bấm Tiếp tục bộ chờ / thử lại bộ lỗi. '
                    'Chưa chạy sang bộ kế tiếp.')
                return
            self._dispatch_next()

        def _record(self, page, ok, detail):
            page._queue_state = "done" if ok else "failed"
            self._results.append((page._series_name, ok, detail))
            self._autosave_session()
            self.series_tabs.setTabText(self.series_tabs.indexOf(page), page._series_name + (' · Xong' if ok else ' · Lỗi'))
            self.series_tabs.setTabToolTip(self.series_tabs.indexOf(page), detail)
            page._log(('✅ ' if ok else '❌ ') + detail)

        def _stop_queue_after_current(self):
            self._stop_after_current = True
            self.btn_pause_series.setEnabled(False)
            self.queue_status.setText('Sẽ dừng hàng đợi sau khi bộ đang chạy hoàn tất. Các bộ còn lại chưa chạy.')

        def _finish_queue(self):
            self._queue_timer.stop()
            self._queue_running = False
            self._active_series = None
            self.btn_add_series.setEnabled(True)
            self.btn_add_folders.setEnabled(True)
            self.btn_open_session.setEnabled(True)
            self.btn_resume_series.setEnabled(True)
            self.btn_run_series.setEnabled(True)
            self.btn_pause_series.setEnabled(False)
            for page in self._pages:
                page.setEnabled(True)
            summary = '\n'.join(f'{name}: {"Xong" if ok else "Lỗi"} — {detail}' for name, ok, detail in self._results)
            if self._pending:
                summary += f'\nChưa chạy: {len(self._pending)} bộ.'
            self.queue_status.setText(summary or 'Hàng đợi đã dừng.')

        def _autosave_session(self):
            if self._session_path:
                try: save_session(self._session_path, self._pages)
                except Exception as exc: self.queue_details.append(f"Không lưu được phiên: {exc}")

        def _save_session_dialog(self):
            if self._session_loading: return
            path, _ = QFileDialog.getSaveFileName(self, "Lưu các bộ và hàng đợi", self._session_path or "BOOM_session.json", "Phiên BOOM (*.json)")
            if path:
                try:
                    save_session(path, self._pages)
                    self._session_path = path
                    self.queue_status.setText("Đã lưu phiên. Tiến độ từng bộ sẽ tự lưu vào file này khi chạy.")
                except Exception as exc: QMessageBox.warning(self, "Lỗi lưu phiên", str(exc))

        def _open_session_dialog(self):
            if self._queue_running or self._session_loading or any(live_workers(p) or getattr(p,'_bulk_loading',False) for p in self._pages): return
            path, _ = QFileDialog.getOpenFileName(self, "Mở phiên (thêm vào các tab hiện có)", "", "Phiên BOOM (*.json)")
            if not path: return
            try: rows = read_session(path)
            except Exception as exc:
                QMessageBox.warning(self, "Không mở được phiên", str(exc)); return
            # Append so that opening a session never discards unsaved current work.
            self._session_loading = True
            self._restore_rows = list(rows)
            self._restore_current = None
            for b in (self.btn_open_session,self.btn_run_series,self.btn_resume_series,
                      self.btn_add_series,self.btn_add_folders): b.setEnabled(False)
            self._session_path = path
            self._restore_session_tick()

        def _restore_session_tick(self):
            try:
                self._restore_session_step()
            except Exception as exc:
                for p in self._pages: p._bulk_loading = False
                self._session_loading = False
                for b in (self.btn_open_session,self.btn_run_series,self.btn_resume_series,
                      self.btn_add_series,self.btn_add_folders): b.setEnabled(True)
                self.queue_status.setText(f"Khôi phục phiên chưa hoàn tất: {exc}. Các tab đã nạp được giữ lại.")

        def _restore_session_step(self):
            if self._restore_current is None:
                if not self._restore_rows:
                    self._session_loading = False
                    for b in (self.btn_open_session,self.btn_run_series,self.btn_resume_series,
                      self.btn_add_series,self.btn_add_folders): b.setEnabled(True)
                    self.queue_status.setText("Đã mở phiên. Bấm Tiếp tục bộ chờ / thử lại bộ lỗi để kiểm tra và chạy lại các bộ chưa hoàn tất.")
                    return
                row = self._restore_rows.pop(0)
                self._add_series()
                page = self._pages[-1]
                page._series_name = str(row.get('name','Bộ phim'))
                state = row.get('state','waiting')
                page._queue_state = 'waiting' if state == 'running' else state
                page._bulk_loading = True
                restore_controls(page, row.get('render',{}))
                if getattr(page,'dub_feature_tab',None): restore_controls(page.dub_feature_tab,row.get('dub',{}))
                page._default_design_template = row.get('default_design') or page._default_design()
                page._part_badge_x = float(row.get('thumbnail_x',.5))
                page._part_badge_y = float(row.get('thumbnail_y',.55))
                page._thumb_src_path = row.get('thumb_source')
                page._thumb_srt_path = row.get('thumb_srt')
                self._restore_current = (page, list(row['cards']))
            page, cards = self._restore_current
            for _ in range(min(8,len(cards))):
                entry = cards.pop(0)
                c = page._add_card(entry['video'],entry.get('srt'))
                if c:
                    c.design_config = entry.get('design') or page._default_design()
                    c.chk_select.setChecked(bool(entry.get('selected',True)))
                    # Phiên cũ lưu đường dẫn video nguồn. Nếu tập đã xuất đủ bộ
                    # 3 file, đổi card sang *_video_sub.mp4 ngay khi khôi phục.
                    # Thumbnail vẫn được cơ chế lazy-load nạp khi card hiện ra,
                    # tránh khởi chạy hàng trăm ffmpeg cùng lúc.
                    c.refresh_srt_from_disk(load_thumbnail=False)
            if not cards:
                page._bulk_loading = False
                page._relayout_grid()
                page._update_run_label()
                self.series_tabs.setTabText(self.series_tabs.indexOf(page), page._series_name + ' · ' + page._queue_state)
                if page.cards: page._on_card_clicked(page.cards[0])
                self._restore_current = None
            QTimer.singleShot(0,self._restore_session_tick)

        def closeEvent(self, event):
            if self._queue_running or any(live_workers(p) for p in self._pages):
                self.queue_status.setText('Đang xử lý. Chọn Dừng sau bộ này và chờ xong trước khi đóng.')
                event.ignore()
            else:
                self._autosave_session()
                super().closeEvent(event)

    return MultiSeriesRenderWidget
