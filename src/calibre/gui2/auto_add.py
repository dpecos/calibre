#!/usr/bin/env python
# License: GPLv3 Copyright: 2015, Kovid Goyal <kovid at kovidgoyal.net>

import os
import shutil
import tempfile
import time
from threading import Event, Thread

from qt.core import QApplication, QCursor, QFileSystemWatcher, QObject, Qt, QTimer, pyqtSignal

from calibre import prints
from calibre.db.adding import compile_rule, filter_filename
from calibre.ebooks import BOOK_EXTENSIONS
from calibre.gui2 import gprefs
from calibre.gui2.dialogs.duplicates import DuplicatesQuestion
from calibre.utils.filenames import make_long_path_useable
from calibre.utils.tdir_in_cache import tdir_in_cache

AUTO_ADDED = frozenset(BOOK_EXTENSIONS) - {'pdr', 'mbp', 'tan'}


class AllAllowed:

    def __init__(self):
        self.disallowed = frozenset(gprefs['blocked_auto_formats'])

    def __contains__(self, x):
        return x not in self.disallowed


def allowed_formats():
    ' Return an object that can be used to test if a format (lowercase) is allowed for auto-adding '
    if gprefs['auto_add_everything']:
        allowed = AllAllowed()
    else:
        allowed = AUTO_ADDED - frozenset(gprefs['blocked_auto_formats'])
    return allowed


class Worker(Thread):

    def __init__(self, path, callback):
        Thread.__init__(self)
        self.daemon = True
        self.keep_running = True
        self.wake_up = Event()
        self.path, self.callback = path, callback
        self.staging = set()
        self.allowed = allowed_formats()
        self.read_rules()

    def read_rules(self):
        try:
            self.compiled_rules = tuple(map(compile_rule, gprefs.get('add_filter_rules', ())))
        except Exception:
            self.compiled_rules = ()
            import traceback
            traceback.print_exc()

    def is_filename_allowed(self, filename):
        allowed = filter_filename(self.compiled_rules, filename)
        if allowed is None:
            ext = os.path.splitext(filename)[1][1:].lower()
            allowed = ext in self.allowed
        return allowed

    def run(self):
        self.tdir = tdir_in_cache('aa')
        try:
            while self.keep_running:
                self.wake_up.wait()
                self.wake_up.clear()
                if not self.keep_running:
                    break
                try:
                    self.auto_add()
                except Exception:
                    import traceback
                    traceback.print_exc()
        finally:
            shutil.rmtree(self.tdir, ignore_errors=True)

    def auto_add(self):
        from calibre.ebooks.metadata.meta import metadata_from_filename
        from calibre.ebooks.metadata.opf2 import metadata_to_opf
        from calibre.utils.ipc.simple_worker import WorkerError, fork_job

        def join(*x):
            return make_long_path_useable(os.path.join(*x))

        files = [x for x in os.listdir(join(self.path)) if
                    # Must not be in the process of being added to the db
                    x not in self.staging and
                    # Firefox creates 0 byte placeholder files when downloading
                    os.stat(join(self.path, x)).st_size > 0 and
                    # Must be a file
                    os.path.isfile(join(self.path, x)) and
                    # Must have read and write permissions
                    os.access(join(self.path, x), os.R_OK|os.W_OK) and
                    # Must be a known ebook file type
                    self.is_filename_allowed(x)
                ]
        data = []
        # Give any in progress copies time to complete
        time.sleep(2)

        def safe_mtime(x):
            try:
                return os.path.getmtime(join(self.path, x))
            except OSError:
                return time.time()

        for fname in sorted(files, key=safe_mtime):
            f = os.path.join(self.path, fname)

            # Try opening the file for reading, if the OS prevents us, then at
            # least on windows, it means the file is open in another
            # application for writing. We will get notified by
            # QFileSystemWatcher when writing is completed, so ignore for now.
            try:
                open(make_long_path_useable(f), 'rb').close()
            except Exception:
                continue
            tdir = tempfile.mkdtemp(dir=self.tdir)
            try:
                fork_job('calibre.ebooks.metadata.meta',
                        'forked_read_metadata', (f, tdir), no_output=True)
            except WorkerError as e:
                prints('Failed to read metadata from:', fname)
                prints(e.orig_tb)
            except Exception:
                import traceback
                traceback.print_exc()

            # Ensure that the pre-metadata file size is present. If it isn't,
            # write 0 so that the file is rescanned
            szpath = os.path.join(tdir, 'size.txt')
            try:
                with open(szpath, 'rb') as f:
                    int(f.read())
            except Exception:
                with open(szpath, 'wb') as f:
                    f.write(b'0')

            opfpath = os.path.join(tdir, 'metadata.opf')
            try:
                if os.stat(opfpath).st_size < 30:
                    raise Exception('metadata reading failed')
            except Exception:
                mi = metadata_from_filename(fname)
                with open(opfpath, 'wb') as f:
                    f.write(metadata_to_opf(mi))
            self.staging.add(fname)
            data.append((fname, tdir))
        if data:
            self.callback(data)


class AutoAdder(QObject):

    metadata_read = pyqtSignal(object)
    auto_convert = pyqtSignal(object)

    def __init__(self, path, parent):
        QObject.__init__(self, parent)
        if path and os.path.isdir(path) and os.access(path, os.R_OK|os.W_OK):
            self.watcher = QFileSystemWatcher(self)
            self.worker = Worker(path, self.metadata_read.emit)
            self.watcher.directoryChanged.connect(self.dir_changed,
                    type=Qt.ConnectionType.QueuedConnection)
            self.metadata_read.connect(self.add_to_db,
                    type=Qt.ConnectionType.QueuedConnection)
            QTimer.singleShot(2000, self.initialize)
            self.auto_convert.connect(self.do_auto_convert,
                    type=Qt.ConnectionType.QueuedConnection)
        elif path:
            prints(path,
                'is not a valid directory to watch for new ebooks, ignoring')

    def read_rules(self):
        if hasattr(self, 'worker'):
            self.worker.read_rules()

    def initialize(self):
        try:
            if os.listdir(self.worker.path):
                self.dir_changed()
        except Exception:
            pass
        self.watcher.addPath(self.worker.path)

    def dir_changed(self, *args):
        if os.path.isdir(self.worker.path) and os.access(self.worker.path,
                os.R_OK|os.W_OK):
            if not self.worker.is_alive():
                self.worker.start()
            self.worker.wake_up.set()

    def stop(self):
        if hasattr(self, 'worker'):
            self.worker.keep_running = False
            self.worker.wake_up.set()

    def wait(self):
        if hasattr(self, 'worker'):
            self.worker.join()

    def __enter__(self):
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))

    def __exit__(self, *args):
        QApplication.restoreOverrideCursor()

    def add_to_db(self, data):
        with self:
            self.do_add(data)

    def do_add(self, data):
        from calibre.ebooks.metadata.opf2 import OPF

        gui = self.parent()
        if gui is None:
            return
        m = gui.library_view.model()
        count = 0

        needs_rescan = False
        duplicates = []
        added_ids = set()

        for fname, tdir in data:
            path_to_remove = os.path.join(self.worker.path, fname)
            paths = [path_to_remove]
            fpath = os.path.join(tdir, 'file_changed_by_plugins')
            if os.path.exists(fpath):
                with open(fpath) as f:
                    paths[0] = f.read()
            book_fmt = os.path.splitext(os.path.basename(paths[0]))[1][1:].upper()
            sz = os.path.join(tdir, 'size.txt')
            try:
                with open(sz, 'rb') as f:
                    sz = int(f.read())
                if sz != os.stat(make_long_path_useable(paths[0])).st_size:
                    raise Exception('Looks like the file was written to after'
                            ' we tried to read metadata')
            except Exception:
                needs_rescan = True
                try:
                    self.worker.staging.remove(fname)
                except KeyError:
                    pass

                continue

            mi = os.path.join(tdir, 'metadata.opf')
            if not os.access(mi, os.R_OK):
                continue
            mi = OPF(open(mi, 'rb'), tdir, populate_spine=False).to_book_metadata()
            if gprefs.get('tag_map_on_add_rules'):
                from calibre.ebooks.metadata.tag_mapper import map_tags
                mi.tags = map_tags(mi.tags, gprefs['tag_map_on_add_rules'])
            if gprefs.get('author_map_on_add_rules'):
                from calibre.ebooks.metadata.author_mapper import compile_rules, map_authors
                new_authors = map_authors(mi.authors, compile_rules(gprefs['author_map_on_add_rules']))
                if new_authors != mi.authors:
                    mi.authors = new_authors
                    mi.author_sort = gui.current_db.new_api.author_sort_from_authors(mi.authors)
            mi = [mi]
            dups, ids = m.add_books(paths, [book_fmt], mi, add_duplicates=not gprefs['auto_add_check_for_duplicates'], return_ids=True)
            added_ids |= set(ids)
            num = len(ids)
            if dups:
                path = dups[0][0]
                with open(os.path.join(tdir, 'dup_cache.'+dups[1][0].lower()),
                        'wb') as dest, open(path, 'rb') as src:
                    shutil.copyfileobj(src, dest)
                    dups[0][0] = dest.name
                duplicates.append(dups)

            try:
                os.remove(make_long_path_useable(path_to_remove))
                self.worker.staging.remove(fname)
            except Exception:
                import traceback
                traceback.print_exc()
            count += num

        if duplicates:
            paths, formats, metadata = [], [], []
            for p, f, mi in duplicates:
                paths.extend(p)
                formats.extend(f)
                metadata.extend(mi)
            dups = [(mic, mic.cover, [p]) for mic, p in zip(metadata, paths)]
            d = DuplicatesQuestion(m.db, dups, parent=gui)
            dups = tuple(d.duplicates)
            if dups:
                paths, formats, metadata = [], [], []
                for mi, cover, book_paths in dups:
                    paths.extend(book_paths)
                    formats.extend([p.rpartition('.')[-1] for p in book_paths])
                    metadata.extend([mi for i in book_paths])
                ids = m.add_books(paths, formats, metadata,
                        add_duplicates=True, return_ids=True)[1]
                added_ids |= set(ids)
                num = len(ids)
                count += num

        for fname, tdir in data:
            try:
                shutil.rmtree(tdir)
            except Exception:
                pass

        if added_ids and gprefs['auto_add_auto_convert']:
            self.auto_convert.emit(added_ids)

        if count > 0:
            m.books_added(count)
            gui.status_bar.show_message(
                (_('Added a book automatically from {src}') if count == 1 else _('Added {num} books automatically from {src}')).format(
                    num=count, src=self.worker.path), 2000)
            gui.refresh_cover_browser()

        if needs_rescan:
            QTimer.singleShot(2000, self.dir_changed)

    def do_auto_convert(self, added_ids):
        gui = self.parent()
        gui.iactions['Convert Books'].auto_convert_auto_add(added_ids)
