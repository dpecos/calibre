__license__   = 'GPL v3'
__copyright__ = '2010-2012, Timothy Legge <timlegge at gmail.com> and David Forrester <davidfor@internode.on.net>'
__docformat__ = 'restructuredtext en'

import os
import time

from calibre import isbytestring
from calibre.constants import DEBUG, preferred_encoding
from calibre.devices.usbms.books import Book as Book_
from calibre.devices.usbms.books import CollectionsBookList
from calibre.ebooks.metadata import author_to_author_sort
from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.book.formatter import SafeFormat
from calibre.prints import debug_print
from calibre.utils.config_base import prefs


class Book(Book_):

    def __init__(self, prefix, lpath, title=None, authors=None, mime=None, date=None, ContentType=None,
                 thumbnail_name=None, size=None, other=None):
        from calibre.utils.date import parse_date
        # debug_print('Book::__init__ - title=', title)
        show_debug = title is not None and title.lower().find('xxxxx') >= 0
        if other is not None:
            other.title = title
            other.published_date = date
        if show_debug:
            debug_print('Book::__init__ - title=', title, 'authors=', authors)
            debug_print('Book::__init__ - other=', other)
        super().__init__(prefix, lpath, size, other)

        if title is not None and len(title) > 0:
            self.title = title

        if authors is not None and len(authors) > 0:
            self.authors_from_string(authors)
            if self.author_sort is None or self.author_sort == 'Unknown':
                self.author_sort = author_to_author_sort(authors)

        self.mime = mime

        self.size = size  # will be set later if None

        if ContentType == '6' and date is not None:
            try:
                self.datetime = time.strptime(date, '%Y-%m-%dT%H:%M:%S.%f')
            except Exception:
                try:
                    self.datetime = time.strptime(date.split('+')[0], '%Y-%m-%dT%H:%M:%S')
                except Exception:
                    try:
                        self.datetime = time.strptime(date.split('+')[0], '%Y-%m-%d')
                    except Exception:
                        try:
                            self.datetime = parse_date(date,
                                    assume_utc=True).timetuple()
                        except Exception:
                            try:
                                self.datetime = time.gmtime(os.path.getctime(self.path))
                            except Exception:
                                self.datetime = time.gmtime()

        self.kobo_metadata = Metadata(title, self.authors)
        self.contentID          = None
        self.current_shelves    = []
        self.kobo_collections   = []
        self.can_put_on_shelves = True
        self.kobo_series        = None
        self.kobo_series_number = None  # Kobo stores the series number as string. And it can have a leading "#".
        self.kobo_series_id     = None
        self.kobo_subtitle      = None
        self.kobo_bookstats     = {}

        if thumbnail_name is not None:
            self.thumbnail = ImageWrapper(thumbnail_name)

        if show_debug:
            debug_print('Book::__init__ end - self=', self)
            debug_print('Book::__init__ end - title=', title, 'authors=', authors)

    @property
    def is_sideloaded(self):
        # If we don't have a content Id, we don't know what type it is.
        return self.contentID and self.contentID.startswith('file')

    @property
    def has_kobo_series(self):
        return self.kobo_series is not None

    @property
    def is_purchased_kepub(self):
        return self.contentID and not self.contentID.startswith('file')

    def __str__(self):
        '''
        A string representation of this object, suitable for printing to
        console
        '''
        ans = ['Kobo metadata:']

        def fmt(x, y):
            ans.append(f'{x:<20}: {y}')

        if self.contentID:
            fmt('Content ID', self.contentID)
        if self.kobo_series:
            fmt('Kobo Series', self.kobo_series + f' #{self.kobo_series_number}')
        if self.kobo_series_id:
            fmt('Kobo Series ID', self.kobo_series_id)
        if self.kobo_subtitle:
            fmt('Subtitle', self.kobo_subtitle)
        if self.mime:
            fmt('MimeType', self.mime)

        ans.append(str(self.kobo_metadata))

        ans = '\n'.join(ans)

        return super().__str__() + '\n' + ans


class ImageWrapper:

    def __init__(self, image_path):
        self.image_path = image_path


class KTCollectionsBookList(CollectionsBookList):

    def __init__(self, oncard, prefix, settings):
        super().__init__(oncard, prefix, settings)
        self.set_device_managed_collections([])

    def get_collections(self, collection_attributes, collections_template=None, template_globals=None):
        debug_print('KTCollectionsBookList:get_collections - start - collection_attributes=', collection_attributes)

        collections = {}

        ca = []
        for c in collection_attributes:
            ca.append(c.lower())
        collection_attributes = ca
        debug_print('KTCollectionsBookList:get_collections - collection_attributes=', collection_attributes)

        for book in self:
            tsval = book.get('title_sort', book.title)
            if tsval is None:
                tsval = book.title

            show_debug = self.is_debugging_title(tsval) or tsval is None
            if show_debug:  # or len(book.device_collections) > 0:
                debug_print('KTCollectionsBookList:get_collections - tsval=', tsval, 'book.title=', book.title, 'book.title_sort=', book.title_sort)
                debug_print('KTCollectionsBookList:get_collections - book.device_collections=', book.device_collections)
                # debug_print(book)
            # Make sure we can identify this book via the lpath
            lpath = getattr(book, 'lpath', None)
            if lpath is None:
                continue
            # If the book is not in the current library, we don't want to use the metadata for the collections
            # or it is a book that cannot be put in a collection (such as recommendations or previews)
            if book.application_id is None or not book.can_put_on_shelves:
                # debug_print("KTCollectionsBookList:get_collections - Book not in current library or cannot be put in a collection")
                continue

            # Decide how we will build the collections. The default: leave the
            # book in all existing collections. Do not add any new ones.
            attrs = ['device_collections']
            if getattr(book, '_new_book', False):
                debug_print('KTCollectionsBookList:get_collections - sending new book')
                if prefs['manage_device_metadata'] == 'manual':
                    # Ensure that the book is in all the book's existing
                    # collections plus all metadata collections
                    attrs += collection_attributes
                else:
                    # For new books, both 'on_send' and 'on_connect' do the same
                    # thing. The book's existing collections are ignored. Put
                    # the book in collections defined by its metadata.
                    attrs = list(collection_attributes)
            elif prefs['manage_device_metadata'] == 'on_connect':
                # For existing books, modify the collections only if the user
                # specified 'on_connect'
                attrs = list(collection_attributes)
                for cat_name in self.device_managed_collections:
                    if cat_name in book.device_collections:
                        if cat_name not in collections:
                            collections[cat_name] = {}
                            if show_debug:
                                debug_print('KTCollectionsBookList:get_collections - Device Managed Collection:', cat_name)
                        if lpath not in collections[cat_name]:
                            collections[cat_name][lpath] = book
                            if show_debug:
                                debug_print('KTCollectionsBookList:get_collections - Device Managed Collection -added book to cat_name', cat_name)
                book.device_collections = []
            if show_debug:
                debug_print('KTCollectionsBookList:get_collections - attrs=', attrs)

            if collections_template is not None:
                attrs.append('%template%')

            for attr in attrs:
                fm = None
                attr = attr.strip()
                if show_debug:
                    debug_print(f"KTCollectionsBookList:get_collections - attr='{attr}'")

                # If attr is device_collections, then we cannot use
                # format_field, because we don't know the fields where the
                # values came from.
                if attr == 'device_collections':
                    doing_dc = True
                    val = book.device_collections  # is a list
                    if show_debug:
                        debug_print('KTCollectionsBookList:get_collections - adding book.device_collections', book.device_collections)
                elif attr == '%template%':
                    doing_dc = False
                    val = ''
                    if collections_template is not None:
                        nv = SafeFormat().safe_format(collections_template, book,
                                                      'KOBO', book, global_vars=template_globals)
                        if show_debug:
                            debug_print('KTCollectionsBookList:get_collections collections_template - result', nv)
                        if nv:
                            val = [v.strip() for v in nv.split(':@:') if v.strip()]
                else:
                    doing_dc = False
                    ign, val, orig_val, fm = book.format_field_extended(attr)
                    val = book.get(attr, None)
                    if show_debug:
                        debug_print('KTCollectionsBookList:get_collections - not device_collections')
                        debug_print('          ign=', ign, ', val=', val, ' orig_val=', orig_val, 'fm=', fm)
                        debug_print('          val=', val)

                if not val:
                    continue
                if isbytestring(val):
                    val = val.decode(preferred_encoding, 'replace')
                if isinstance(val, (list, tuple)):
                    val = list(val)
                    # debug_print("KTCollectionsBookList:get_collections - val is list=", val)
                elif fm is not None and fm['datatype'] == 'series':
                    val = [orig_val]
                elif fm is not None and fm['datatype'] == 'rating':
                    val = [str(orig_val / 2.0)]
                elif fm is not None and fm['datatype'] == 'text' and fm['is_multiple']:
                    if isinstance(orig_val, (list, tuple)):
                        val = orig_val
                    else:
                        val = [orig_val]
                    if show_debug:
                        debug_print('KTCollectionsBookList:get_collections - val is text and multiple', val)
                elif fm is not None and fm['datatype'] == 'composite' and fm['is_multiple']:
                    if show_debug:
                        debug_print('KTCollectionsBookList:get_collections - val is compositeand multiple', val)
                    val = [v.strip() for v in
                           val.split(fm['is_multiple']['ui_to_list'])]
                else:
                    val = [val]
                if show_debug:
                    debug_print('KTCollectionsBookList:get_collections - val=', val)

                for category in val:
                    # debug_print("KTCollectionsBookList:get_collections - category=", category)
                    if doing_dc:
                        pass  # No need to do anything with device_collections
                    elif fm is not None and fm['is_custom']:  # is a custom field
                        if fm['datatype'] == 'text' and len(category) > 1 and \
                                category[0] == '[' and category[-1] == ']':
                            continue
                    else:                       # is a standard field
                        if attr == 'tags' and len(category) > 1 and \
                                category[0] == '[' and category[-1] == ']':
                            continue

                    # The category should not be None, but, it has happened.
                    if not category:
                        continue

                    cat_name = str(category).strip(' ,')

                    if cat_name not in collections:
                        collections[cat_name] = {}
                        if show_debug:
                            debug_print('KTCollectionsBookList:get_collections - created collection for cat_name', cat_name)
                    if lpath not in collections[cat_name]:
                        collections[cat_name][lpath] = book
                        if show_debug:
                            debug_print('KTCollectionsBookList:get_collections - added book to collection for cat_name', cat_name)
                    if show_debug:
                        debug_print('KTCollectionsBookList:get_collections - cat_name', cat_name)

        # Sort collections
        result = {}

        for category, lpaths in collections.items():
            result[category] = lpaths.values()
        # debug_print("KTCollectionsBookList:get_collections - result=", result.keys())
        debug_print('KTCollectionsBookList:get_collections - end')
        return result

    def set_device_managed_collections(self, collection_names):
        self.device_managed_collections = collection_names

    def set_debugging_title(self, title):
        self.debugging_title = title

    def is_debugging_title(self, title):
        if not DEBUG:
            return False
        # debug_print("KTCollectionsBookList:is_debugging - title=", title, "self.debugging_title=", self.debugging_title)
        is_debugging = self.debugging_title is not None and len(self.debugging_title) > 0 and title is not None and (
            title.lower().find(self.debugging_title.lower()) >= 0 or len(title) == 0)
        # debug_print("KTCollectionsBookList:is_debugging - is_debugging=", is_debugging)

        return is_debugging
