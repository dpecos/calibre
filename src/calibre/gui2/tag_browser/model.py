#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2011, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import copy
import os
import traceback
from collections import OrderedDict, namedtuple

from qt.core import QAbstractItemModel, QFont, QIcon, QMimeData, QModelIndex, QObject, Qt, pyqtSignal

from calibre.constants import config_dir
from calibre.db.categories import Tag, category_display_order
from calibre.db.constants import TEMPLATE_ICON_INDICATOR
from calibre.ebooks.metadata import rating_to_stars
from calibre.gui2 import config, error_dialog, file_icon_provider, gprefs, question_dialog
from calibre.gui2.dialogs.confirm_delete import confirm
from calibre.library.field_metadata import category_icon_map
from calibre.utils.config import prefs, tweaks
from calibre.utils.formatter import EvalFormatter
from calibre.utils.icu import collation_order_for_partitioning, contains, lower, primary_contains, primary_strcmp, sort_key, strcmp
from calibre.utils.icu import lower as icu_lower
from calibre.utils.icu import upper as icu_upper
from calibre.utils.serialize import json_dumps, json_loads
from polyglot.builtins import iteritems, itervalues

TAG_SEARCH_STATES = {'clear': 0, 'mark_plus': 1, 'mark_plusplus': 2,
                     'mark_minus': 3, 'mark_minusminus': 4}
DRAG_IMAGE_ROLE = Qt.ItemDataRole.UserRole + 1000
COUNT_ROLE = DRAG_IMAGE_ROLE + 1

_bf = None


def bf():
    global _bf
    if _bf is None:
        _bf = QFont()
        _bf.setBold(True)
        _bf = (_bf)
    return _bf


class TagTreeItem:  # {{{

    CATEGORY = 0
    TAG      = 1
    ROOT     = 2
    category_custom_icons = {}
    value_icons = {}
    value_icon_cache = {}
    icon_config_dir = {}
    file_icon_provider = None
    eval_formatter = EvalFormatter()

    def __init__(self, data=None, is_category=False, icon_map=None,
                 parent=None, tooltip=None, category_key=None, temporary=False,
                 is_gst=False):
        if self.file_icon_provider is None:
            self.file_icon_provider = TagTreeItem.file_icon_provider = file_icon_provider().icon_from_ext
        self.parent = parent
        self.children = []
        self.blank = QIcon()
        self.is_gst = is_gst
        self.icon = None
        self.boxed = False
        self.temporary = False
        self.can_be_edited = False
        self.icon_state_map = list(icon_map)
        if self.parent is not None:
            self.parent.append(self)

        if data is None:
            self.type = self.ROOT
        else:
            self.type = self.CATEGORY if is_category else self.TAG

        if self.type == self.CATEGORY:
            self.name = data
            self.py_name = data
            self.category_key = category_key
            self.temporary = temporary
            self.tag = Tag(data, category=category_key,
                   is_editable=category_key not in
                            ['news', 'search', 'identifiers', 'languages'],
                   is_searchable=category_key not in ['search'])
        elif self.type == self.TAG:
            self.tag = data
            self.cached_average_rating = None
            self.cached_item_count = None

        self.tooltip = tooltip or ''

    @property
    def name_id(self):
        if self.type == self.CATEGORY:
            return self.category_key + ':' + self.name
        elif self.type == self.TAG:
            return self.tag.original_name
        return ''

    def break_cycles(self):
        del self.parent
        del self.children

    def root_node(self):
        p = self
        while p.parent.type != self.ROOT:
            p = p.parent
        return p

    def ensure_icon(self):
        if self.icon_state_map[0] is not None:
            return
        cc = None
        if self.type == self.TAG  and gprefs['tag_browser_show_value_icons']:
            if self.tag.category == 'formats':
                fmt = self.tag.original_name.replace('ORIGINAL_', '')
                cc = self.file_icon_provider(fmt)
            else:
                if self.is_gst:
                    cc = self.category_custom_icons.get(self.root_node().category_key, None)
                elif self.tag.category == 'search' and not self.tag.is_searchable:
                    cc = self.category_custom_icons.get('search_folder', None)
                else:
                    if self.icon is None:
                        node = self
                        val_icon = None
                        category = node.tag.category
                        if category in self.value_icons:
                            while True:
                                val_icon = self.value_icons.get(category, {}).get(node.tag.original_name)
                                if val_icon is not None:
                                    # Have an icon. Use it if value exact match or
                                    # it applies to children
                                    if node != self and not val_icon[1]:
                                        val_icon = None
                                    break
                                node = node.parent
                                if node.type != self.TAG or node.type == self.ROOT:
                                    break
                            if val_icon is None and TEMPLATE_ICON_INDICATOR in self.value_icons[category]:
                                v = {'category': category, 'value': self.tag.original_name,
                                     'count': getattr(self.tag, 'count', ''),
                                     'avg_rating': getattr(self.tag, 'avg_rating', '')}
                                from calibre.gui2.ui import get_gui
                                db = get_gui().current_db
                                t = self.eval_formatter.safe_format(
                                    self.value_icons[category][TEMPLATE_ICON_INDICATOR][0], v, 'VALUE_ICON_TEMPLATE_ERROR', {}, database=db)
                                if t:
                                    val_icon = (os.path.join('template_icons', t), False)
                                else:
                                    val_icon = None
                        if val_icon is not None:
                            cc = self.value_icon_cache.get(val_icon[0])
                            if cc is None:
                                cc = QIcon.ic(os.path.join(self.icon_config_dir, val_icon[0]))
                                if cc.isNull():
                                    cc = self.category_custom_icons.get(self.tag.category, None)
                                self.value_icon_cache[val_icon[0]] = cc
                            self.icon = cc
                        else:
                            cc = self.category_custom_icons.get(self.tag.category, None)
                    else:
                        cc = self.icon
        elif self.type == self.CATEGORY:
            if self.parent.type == self.ROOT:
                if gprefs['tag_browser_show_category_icons']:
                    cc = self.category_custom_icons.get(self.category_key, None)
            else:
                if gprefs['tag_browser_show_value_icons']:
                    cc = self.category_custom_icons.get(self.category_key, None)
        self.icon_state_map[0] = cc or QIcon()

    def __str__(self):
        if self.type == self.ROOT:
            return 'ROOT'
        if self.type == self.CATEGORY:
            return f'CATEGORY(category_key={self.category_key!r}, name={self.name!r}, num_children={len(self.children)!r}, temp={self.temporary!r})'
        return f'TAG(name={self.tag.name!r}), temp={self.temporary!r})'

    def row(self):
        if self.parent is not None:
            return self.parent.children.index(self)
        return 0

    def append(self, child):
        child.parent = self
        self.children.append(child)

    @property
    def average_rating(self):
        if self.type != self.TAG:
            return 0
        if self.tag.category == 'search':
            return None
        if not self.tag.is_hierarchical:
            return self.tag.avg_rating
        if not self.children:
            return self.tag.avg_rating  # leaf node, avg_rating is correct
        if self.cached_average_rating is None:
            raise ValueError('Must compute average rating for tag ' + self.tag.original_name)
        return self.cached_average_rating

    @property
    def item_count(self):
        if not self.tag.is_hierarchical or not self.children:
            return self.tag.count
        if self.cached_item_count is not None:
            return self.cached_item_count

        def child_item_set(node):
            s = node.tag.id_set.copy()
            for child in node.children:
                s |= child_item_set(child)
            return s
        self.cached_item_count = len(child_item_set(self))
        return self.cached_item_count

    def data(self, role):
        if role == Qt.ItemDataRole.UserRole:
            return self
        if self.type == self.TAG:
            return self.tag_data(role)
        if self.type == self.CATEGORY:
            return self.category_data(role)
        return None

    def category_data(self, role):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.py_name
        if role == Qt.ItemDataRole.EditRole:
            return (self.py_name)
        if role == Qt.ItemDataRole.DecorationRole:
            if not self.tag.state:
                self.ensure_icon()
            return self.icon_state_map[self.tag.state]
        if role == Qt.ItemDataRole.FontRole:
            return bf()
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.tooltip if gprefs['tag_browser_show_tooltips'] else None
        if role == DRAG_IMAGE_ROLE:
            self.ensure_icon()
            return self.icon_state_map[0]
        if role == COUNT_ROLE:
            return len(self.child_tags())
        return None

    def tag_data(self, role):
        tag = self.tag
        if tag.use_sort_as_name:
            name = tag.sort
        else:
            if not tag.is_hierarchical:
                name = tag.original_name
            else:
                name = tag.name
        if role == Qt.ItemDataRole.DisplayRole:
            return str(name)
        if role == Qt.ItemDataRole.EditRole:
            return (tag.original_name)
        if role == Qt.ItemDataRole.DecorationRole:
            if not tag.state:
                self.ensure_icon()
            return self.icon_state_map[tag.state]
        if role == Qt.ItemDataRole.ToolTipRole:
            if gprefs['tag_browser_show_tooltips']:
                if self.type == self.TAG and tag.category == 'search':
                    if tag.search_expression is None:
                        return _('{} is not a saved search').format(tag.original_name)
                    return (f'search:{tag.original_name}\n' +
                            _('Search expression:') + ' ' + tag.search_expression)
                tt = [self.tooltip] if self.tooltip else []
                if tag.original_categories:
                    tt.append('{}:{}'.format(','.join(tag.original_categories), tag.original_name))
                else:
                    tt.append(f'{tag.category}:{tag.original_name}')
                ar = self.average_rating
                if ar:
                    tt.append(_('Average rating for books in this category: %.1f') % ar)
                elif self.type == self.TAG:
                    if ar is not None:
                        tt.append(_('Books in this category are unrated'))
                    if tag.category != 'search':
                        tt.append(_('Number of books: %s') % self.item_count)
                    from calibre.gui2.ui import get_gui
                    db = get_gui().current_db.new_api
                    link = (None if not db.has_link_map(tag.category)
                                 else db.get_link_map(tag.category).get(tag.original_name))
                    if link:
                        tt.append(_('Link: %s') % link)
                return '\n'.join(tt)
            return None
        if role == DRAG_IMAGE_ROLE:
            self.ensure_icon()
            return self.icon_state_map[0]
        if role == COUNT_ROLE:
            return self.item_count
        return None

    def dump_data(self):
        fmt = '%s [count=%s%s]'
        if self.type == self.CATEGORY:
            return fmt % (self.py_name, len(self.child_tags()), '')
        tag = self.tag
        if tag.use_sort_as_name:
            name = tag.sort
        else:
            if not tag.is_hierarchical:
                name = tag.original_name
            else:
                name = tag.name
        count = self.item_count
        rating = self.average_rating
        if rating:
            rating = f',rating={rating:.1f}'
        return fmt % (name, count, rating or '')

    def toggle(self, set_to=None):
        '''
        set_to: None => advance the state, otherwise a value from TAG_SEARCH_STATES
        '''
        tag = self.tag
        if set_to is None:
            while True:
                tag_search_order_graph = gprefs.get('tb_search_order')
                # JSON dumps converts integer keys to strings, so do it explicitly
                tag.state = tag_search_order_graph[str(tag.state)]
                if tag.state == TAG_SEARCH_STATES['mark_plus'] or \
                        tag.state == TAG_SEARCH_STATES['mark_minus']:
                    if tag.is_searchable:
                        break
                elif tag.state == TAG_SEARCH_STATES['mark_plusplus'] or\
                        tag.state == TAG_SEARCH_STATES['mark_minusminus']:
                    if tag.is_searchable and len(self.children) and \
                                    tag.is_hierarchical == '5state':
                        break
                else:
                    break
        else:
            tag.state = set_to

    def all_children(self):
        res = []

        def recurse(nodes, res):
            for t in nodes:
                res.append(t)
                recurse(t.children, res)
        recurse(self.children, res)
        return res

    def child_tags(self):
        res = []

        def recurse(nodes, res, depth):
            if depth > 100:
                return
            for t in nodes:
                if t.type != TagTreeItem.CATEGORY:
                    res.append(t)
                recurse(t.children, res, depth+1)
        recurse(self.children, res, 1)
        return res
    # }}}


FL_Interval = namedtuple('FL_Interval', ('first_chr', 'last_chr', 'length'))


def rename_only_in_vl_question(parent):
    return question_dialog(parent,
                           _('Rename in Virtual library'), '<p>' +
                           _('Do you want this rename to apply only to books '
                             'in the current Virtual library?') + '</p>',
                           yes_text=_('Yes, apply only in VL'),
                           no_text=_('No, apply in entire library'))


class TagsModel(QAbstractItemModel):  # {{{

    search_item_renamed = pyqtSignal()
    tag_item_renamed = pyqtSignal()
    refresh_required = pyqtSignal()
    research_required = pyqtSignal()
    restriction_error = pyqtSignal(object)
    drag_drop_finished = pyqtSignal(object)
    user_categories_edited = pyqtSignal(object, object)
    user_category_added = pyqtSignal()
    show_error_after_event_loop_tick_signal = pyqtSignal(object, object, object)
    convert_requested = pyqtSignal(object, object)

    def __init__(self, parent, prefs=gprefs):
        QAbstractItemModel.__init__(self, parent)
        self.use_position_based_index_on_next_recount = False
        self.prefs = prefs
        self.node_map = {}
        self.category_nodes = []
        self.category_custom_icons = {}
        self.value_icons = {}
        self.value_icon_cache = {}
        self.icon_config_dir = os.path.join(config_dir, 'tb_icons')
        for k, v in iteritems(self.prefs['tags_browser_category_icons']):
            icon = QIcon(os.path.join(self.icon_config_dir, v))
            if len(icon.availableSizes()) > 0:
                self.category_custom_icons[k] = icon
        self.categories_with_ratings = ['authors', 'series', 'publisher', 'tags']
        self.icon_state_map = [None, QIcon.ic('plus.png'), QIcon.ic('plusplus.png'),
                             QIcon.ic('minus.png'), QIcon.ic('minusminus.png')]

        self.hidden_categories = set()
        self.search_restriction = None
        self.filter_categories_by = None
        self.collapse_model = 'disable'
        self.row_map = []
        self.db = None
        self.root_item = self.create_node(icon_map=self.icon_state_map)
        self._build_in_progress = False
        self.reread_collapse_model({}, rebuild=False)
        self.show_error_after_event_loop_tick_signal.connect(self.on_show_error_after_event_loop_tick, type=Qt.ConnectionType.QueuedConnection)
        self.reset_notes_and_link_maps()

    @property
    def gui_parent(self):
        return QObject.parent(self)

    def rename_user_category_icon(self, old_key, new_key):
        '''
        This is required for user categories because the key (lookup name) changes
        on rename. We must rename the old icon to use the new key then update
        the preferences and internal tables.
        '''
        old_icon = self.prefs['tags_browser_category_icons'].get(old_key, None)
        if old_icon is not None:
            old_path = os.path.join(self.icon_config_dir, old_icon)
            _, ext = os.path.splitext(old_path)
            new_icon = new_key + ext
            new_path = os.path.join(self.icon_config_dir, new_icon)
            os.replace(old_path, new_path)
            self.set_custom_category_icon(new_key, new_icon)
            self.set_custom_category_icon(old_key, None)

    def set_value_icon(self, key, value, file_name, children):
        '''
        Add a 'rule' for an icon for a value in the tag browser as a dict entry:
            value_icons[key] = {value: (file_name, children)}

        :param key: the lookup name for the tag browser category
        :param value: the item value in the category. If the value is
                      TEMPLATE_ICON_INDICATOR then the rule applies to all items
                      that don't have a specific rule.
        :param file_name: the name of the icon file to use for this value. If
                          this is a template rule then this is the text of the template.
        :param children: for specific (non-template) rules: if True then the rule
                         is to be used for any children of the item that don't have
                         a specific rule. If False then this rule is used only for
                         the specified item.
        '''
        v = self.value_icons = self.prefs['tags_browser_value_icons']
        if key not in v:
            self.value_icons[key] = {value: (file_name, children)}
        else:
            self.value_icons[key].update({value: (file_name, children)})
        self.value_icon_cache.pop(file_name, None)
        self.prefs['tags_browser_value_icons'] = self.value_icons

    def _remove_icon_file(self, file_name):
        if file_name is not None:
            path = os.path.join(self.icon_config_dir, file_name)
            try:
                os.remove(path)
            except Exception:
                pass

    def remove_value_icon(self, key, value, file_name):
        self.value_icons = self.prefs['tags_browser_value_icons']
        self.value_icons.get(key, {}).pop(value, None)
        self.prefs['tags_browser_value_icons'] = self.value_icons
        self._remove_icon_file(file_name)

    def remove_all_value_icons(self, key, keep_template=True):
        self.value_icons = self.prefs['tags_browser_value_icons']
        values = self.value_icons.pop(key, {})
        self.value_icons[key] = {}
        template = values.pop(TEMPLATE_ICON_INDICATOR, None)
        if keep_template and template is not None:
            self.value_icons[key][TEMPLATE_ICON_INDICATOR] = template
        self.prefs['tags_browser_value_icons'] = self.value_icons
        for file_name,child in values.values():
            self._remove_icon_file(file_name)

    def set_custom_category_icon(self, key, path):
        d = self.prefs['tags_browser_category_icons']
        if path:
            d[key] = path
            self.category_custom_icons[key] = QIcon(os.path.join(self.icon_config_dir, path))
        else:
            self._remove_icon_file(d.pop(key, None))
            self.category_custom_icons.pop(key, None)
        self.prefs['tags_browser_category_icons'] = d

    def reread_collapse_model(self, state_map, rebuild=True):
        if self.prefs['tags_browser_collapse_at'] == 0:
            self.collapse_model = 'disable'
        else:
            self.collapse_model = self.prefs['tags_browser_partition_method']
        if rebuild:
            self.rebuild_node_tree(state_map)

    def set_database(self, db, hidden_categories=None):
        self.beginResetModel()
        self.value_icons = self.prefs['tags_browser_value_icons']
        hidden_cats = db.new_api.pref('tag_browser_hidden_categories', None)
        # migrate from config to db prefs
        if hidden_cats is None:
            hidden_cats = config['tag_browser_hidden_categories']
        self.hidden_categories = set()
        # strip out any non-existent field keys
        for cat in hidden_cats:
            if cat in db.field_metadata:
                self.hidden_categories.add(cat)
        db.new_api.set_pref('tag_browser_hidden_categories', list(self.hidden_categories))
        if hidden_categories is not None:
            self.hidden_categories = hidden_categories

        self.db = db
        self._run_rebuild()
        self.endResetModel()

    def reset_tag_browser(self):
        self.beginResetModel()
        self.value_icon_cache = {}
        self.value_icons = self.prefs['tags_browser_value_icons']
        hidden_cats = self.db.new_api.pref('tag_browser_hidden_categories', {})
        self.hidden_categories = set()
        # strip out any non-existent field keys
        for cat in hidden_cats:
            if cat in self.db.field_metadata:
                self.hidden_categories.add(cat)
        self._run_rebuild()
        self.endResetModel()

    def _cached_notes_map(self, category):
        if self.notes_map is None:
            self.notes_map = {}
        ans = self.notes_map.get(category)
        if ans is None:
            try:
                self.notes_map[category] = ans = (self.db.new_api.get_all_items_that_have_notes(category),
                                            self.db.new_api.get_item_name_map(category))
            except Exception:
                self.notes_map[category] = ans = (frozenset(), {})
        return ans

    def _cached_link_map(self, category):
        if self.link_map is None:
            self.link_map = {}
        ans = self.link_map.get(category)
        if ans is None:
            try:
                self.link_map[category] = ans = self.db.new_api.get_link_map(category)
            except Exception:
                self.link_map[category] = ans = {}
        return ans

    def category_has_notes(self, category):
        return bool(self._cached_notes_map(category)[0])

    def item_has_note(self, category, item_name):
        notes_map, item_id_map = self._cached_notes_map(category)
        return item_id_map.get(item_name) in notes_map

    def category_has_links(self, category):
        return bool(self._cached_link_map(category))

    def item_has_link(self, category, item_name):
        return item_name in self._cached_link_map(category)

    def reset_notes_and_link_maps(self):
        self.link_map = self.notes_map = None

    def rebuild_node_tree(self, state_map={}):
        if self._build_in_progress:
            print('Tag browser build already in progress')
            traceback.print_stack()
            return
        # traceback.print_stack()
        # print()
        self._build_in_progress = True
        self.beginResetModel()
        self._run_rebuild(state_map=state_map)
        self.endResetModel()
        self._build_in_progress = False

    def _run_rebuild(self, state_map={}):
        self.reset_notes_and_link_maps()
        for node in itervalues(self.node_map):
            node.break_cycles()
        del node  # Clear reference to node in the current frame
        self.node_map.clear()
        self.category_nodes = []
        self.hierarchical_categories = {}
        self.root_item = self.create_node(icon_map=self.icon_state_map)
        self._rebuild_node_tree(state_map=state_map)

    def _rebuild_node_tree(self, state_map):
        # Note that _get_category_nodes can indirectly change the
        # user_categories dict.
        data = self._get_category_nodes(config['sort_tags_by'])
        gst = self.db.new_api.pref('grouped_search_terms', {})

        if self.category_custom_icons.get('search_folder', None) is None:
            self.category_custom_icons['search_folder'] = QIcon.ic('folder_saved_search')
        last_category_node = None
        category_node_map = {}
        self.user_category_node_tree = {}

        # We build the node tree including categories that might later not be
        # displayed because their items might be in User categories. The resulting
        # nodes will be reordered later.
        for i, key in enumerate(self.categories):
            is_gst = False
            if key.startswith('@') and key[1:] in gst:
                tt = _('The grouped search term name is "{0}"').format(key)
                is_gst = True
            elif key == 'news':
                tt = ''
            else:
                cust_desc = ''
                fm = self.db.field_metadata[key]
                if fm['is_custom']:
                    cust_desc = fm['display'].get('description', '')
                    if cust_desc:
                        cust_desc = '\n' + _('Description:') + ' ' + cust_desc
                tt = _('The lookup/search name is "{0}"{1}').format(key, cust_desc)

            if self.category_custom_icons.get(key, None) is None:
                self.category_custom_icons[key] = QIcon.ic(
                    category_icon_map['gst'] if is_gst else category_icon_map.get(
                        key, (category_icon_map['user:'] if key.startswith('@') else category_icon_map['custom:'])))

            if key.startswith('@'):
                path_parts = key.split('.')
                path = ''
                last_category_node = self.root_item
                tree_root = self.user_category_node_tree
                for i,p in enumerate(path_parts):
                    path += p
                    if path not in category_node_map:
                        node = self.create_node(parent=last_category_node,
                                   data=p[1:] if i == 0 else p,
                                   is_category=True,
                                   is_gst=is_gst,
                                   tooltip=tt if path == key else path,
                                   category_key=path,
                                   icon_map=self.icon_state_map)
                        last_category_node = node
                        category_node_map[path] = node
                        self.category_nodes.append(node)
                        node.can_be_edited = (not is_gst) and (i == (len(path_parts)-1))
                        if not is_gst:
                            node.tag.is_hierarchical = '5state'
                            tree_root[p] = {}
                            tree_root = tree_root[p]
                    else:
                        last_category_node = category_node_map[path]
                        tree_root = tree_root[p]
                    path += '.'
            else:
                node = self.create_node(parent=self.root_item,
                                   data=self.categories[key],
                                   is_category=True,
                                   is_gst=False,
                                   tooltip=tt, category_key=key,
                                   icon_map=self.icon_state_map)
                category_node_map[key] = node
                last_category_node = node
                self.category_nodes.append(node)
        self._create_node_tree(data, state_map)

    def _create_node_tree(self, data, state_map):
        sort_by = config['sort_tags_by']

        eval_formatter = EvalFormatter()
        intermediate_nodes = {}

        if data is None:
            print('_create_node_tree: no data!')
            traceback.print_stack()
            return

        collapse = self.prefs['tags_browser_collapse_at']
        collapse_model = self.collapse_model
        if collapse == 0:
            collapse_model = 'disable'
        elif collapse_model != 'disable':
            if sort_by == 'name':
                collapse_template = tweaks['categories_collapsed_name_template']
            elif sort_by == 'rating':
                collapse_model = 'partition'
                collapse_template = tweaks['categories_collapsed_rating_template']
            else:
                collapse_model = 'partition'
                collapse_template = tweaks['categories_collapsed_popularity_template']

        def get_name_components(name):
            components = [t.strip() for t in name.split('.') if t.strip()]
            if len(components) == 0 or '.'.join(components) != name:
                components = [name]
            return components

        def process_one_node(category, collapse_model, book_rating_map, state_map):  # {{{
            collapse_letter = None
            key = category.category_key
            is_gst = category.is_gst
            if key not in data:
                return

            # Ensure we use the prefix for any user category. Non UCs can't have
            # a period in the key so doing the partition without an if is safe
            k = key.partition('.')[0]
            # Use old pref if new one doesn't exist
            if k in self.db.prefs.get('tag_browser_dont_collapse',
                                       self.prefs['tag_browser_dont_collapse']):
                collapse_model = 'disable'

            cat_len = len(data[key])
            if cat_len <= 0:
                return

            category_child_map = {}
            fm = self.db.field_metadata[key]
            clear_rating = True if key not in self.categories_with_ratings and \
                                not fm['is_custom'] and \
                                not fm['kind'] == 'user' \
                            else False
            in_uc = fm['kind'] == 'user' and not is_gst
            tt = key if in_uc else None

            if collapse_model == 'first letter':
                # Build a list of 'equal' first letters by noticing changes
                # in ICU's 'ordinal' for the first letter. In this case, the
                # first letter can actually be more than one letter long.
                fl_collapse_when = self.prefs['tags_browser_collapse_fl_at']
                fl_collapse = True if fl_collapse_when > 1 else False
                intervals = []
                cl_list = [None] * len(data[key])
                last_ordnum = 0
                last_c = ' '
                last_idx = 0
                for idx,tag in enumerate(data[key]):
                    # Deal with items that don't have sorts, such as formats
                    t = tag.sort if tag.sort else tag.name
                    c = icu_upper(t) if t else ' '
                    ordnum, ordlen = collation_order_for_partitioning(c)
                    if last_ordnum != ordnum:
                        if fl_collapse and idx > 0:
                            intervals.append(FL_Interval(last_c, last_c, idx-last_idx))
                            last_idx = idx
                        last_c = c[0:ordlen]
                        last_ordnum = ordnum
                    cl_list[idx] = last_c
                if fl_collapse:
                    intervals.append(FL_Interval(last_c, last_c, len(cl_list)-last_idx))
                    # Combine together first letter categories that are smaller
                    # than the specified option. We choose which item to combine
                    # by the size of the items before and after, privileging making
                    # smaller categories. Loop through the intervals doing the combine
                    # until nothing changes. Multiple iterations are required because
                    # we might need to combine categories that are already combined.
                    fl_intervals_changed = True
                    null_interval = FL_Interval('', '', 100000000)
                    while fl_intervals_changed and len(intervals) > 1:
                        fl_intervals_changed = False
                        for idx,interval in enumerate(intervals):
                            if interval.length >= fl_collapse_when:
                                continue
                            prev = next_ = null_interval
                            if idx == 0:
                                next_ = intervals[idx+1]
                            else:
                                prev = intervals[idx-1]
                                if idx < len(intervals) - 1:
                                    next_ = intervals[idx+1]
                            if prev.length < next_.length:
                                intervals[idx-1] = FL_Interval(prev.first_chr,
                                                               interval.last_chr,
                                                               prev.length + interval.length)
                            else:
                                intervals[idx+1] = FL_Interval(interval.first_chr,
                                                               next_.last_chr,
                                                               next_.length + interval.length)
                            del intervals[idx]
                            fl_intervals_changed = True
                            break
                    # Now correct the first letter list, entering either the letter
                    # or the range for each item in the category. If we ended up
                    # with only one 'first letter' category then don't combine
                    # letters and revert to basic 'by first letter'
                    if len(intervals) > 1:
                        cur_idx = 0
                        for interval in intervals:
                            first_chr, last_chr, length = interval
                            for i in range(length):
                                if first_chr == last_chr:
                                    cl_list[cur_idx] = first_chr
                                else:
                                    cl_list[cur_idx] = f'{first_chr} - {last_chr}'
                                cur_idx += 1
            top_level_component = 'z' + data[key][0].original_name

            last_idx = -collapse
            category_is_hierarchical = self.is_key_a_hierarchical_category(key)

            for idx,tag in enumerate(data[key]):
                components = None
                if clear_rating:
                    tag.avg_rating = None
                tag.state = state_map.get((tag.name, tag.category), 0)

                if collapse_model != 'disable' and cat_len > collapse:
                    if collapse_model == 'partition':
                        # Only partition at the top level. This means that we must
                        # not do a break until the outermost component changes.
                        if idx >= last_idx + collapse and \
                                 not tag.original_name.startswith(top_level_component+'.'):
                            if cat_len > idx + collapse:
                                last = idx + collapse - 1
                            else:
                                last = cat_len - 1
                            if category_is_hierarchical:
                                ct = copy.copy(data[key][last])
                                components = get_name_components(ct.original_name)
                                ct.sort = ct.name = components[0]
                                d = {'last': ct}
                                # Do the first node after the last node so that
                                # the components array contains the right values
                                # to be used later
                                ct2 = copy.copy(tag)
                                components = get_name_components(ct2.original_name)
                                ct2.sort = ct2.name = components[0]
                                d['first'] = ct2
                            else:
                                d = {'first': tag}
                                # Some nodes like formats and identifiers don't
                                # have sort set. Fix that so the template will work
                                if d['first'].sort is None:
                                    d['first'].sort = tag.name
                                d['last'] = data[key][last]
                                if d['last'].sort is None:
                                    d['last'].sort = data[key][last].name

                            name = eval_formatter.safe_format(collapse_template,
                                                        d, '##TAG_VIEW##', None)
                            if name.startswith('##TAG_VIEW##'):
                                # Formatter threw an exception. Don't create subnode
                                node_parent = sub_cat = category
                            else:
                                sub_cat = self.create_node(parent=category, data=name,
                                     tooltip=None, temporary=True,
                                     is_category=True,
                                     is_gst=is_gst,
                                     category_key=category.category_key,
                                     icon_map=self.icon_state_map)
                                sub_cat.tag.is_searchable = False
                                node_parent = sub_cat
                            last_idx = idx  # remember where we last partitioned
                        else:
                            node_parent = sub_cat
                    else:  # by 'first letter'
                        cl = cl_list[idx]
                        if cl != collapse_letter:
                            collapse_letter = cl
                            sub_cat = self.create_node(parent=category,
                                     data=collapse_letter,
                                     is_category=True,
                                     is_gst=is_gst,
                                     tooltip=None, temporary=True,
                                     category_key=category.category_key,
                                     icon_map=self.icon_state_map)
                        node_parent = sub_cat
                else:
                    node_parent = category

                # category display order is important here. The following works
                # only if all the non-User categories are displayed before the
                # User categories
                if category_is_hierarchical or tag.is_hierarchical:
                    components = get_name_components(tag.original_name)
                else:
                    components = [tag.original_name]

                if (not tag.is_hierarchical) and (in_uc or
                        (fm['is_custom'] and fm['display'].get('is_names', False)) or
                        not category_is_hierarchical or len(components) == 1):
                    n = self.create_node(parent=node_parent, data=tag, tooltip=tt,
                                    is_gst=is_gst, icon_map=self.icon_state_map)
                    category_child_map[tag.name, tag.category] = n
                else:
                    child_key = key if is_gst else tag.category
                    for i,comp in enumerate(components):
                        if i == 0:
                            child_map = category_child_map
                            top_level_component = comp
                        else:
                            child_map = {(t.tag.name, key if is_gst else t.tag.category):
                                         t for t in node_parent.children
                                            if t.type != TagTreeItem.CATEGORY}
                        if (comp,child_key) in child_map:
                            node_parent = child_map[(comp, child_key)]
                            t = node_parent.tag
                            t.is_hierarchical = '5state' if tag.category != 'search' else '3state'
                            if tag.id_set is not None and t.id_set is not None:
                                t.id_set = t.id_set | tag.id_set
                            intermediate_nodes[t.original_name, child_key] = t
                        else:
                            if i < len(components)-1:
                                original_name = '.'.join(components[:i+1])
                                t = intermediate_nodes.get((original_name, child_key), None)
                                if t is None:
                                    t = copy.copy(tag)
                                    t.original_name = original_name
                                    t.count = 0
                                    if key != 'search':
                                        # This 'manufactured' intermediate node can
                                        # be searched, but cannot be edited.
                                        t.is_editable = False
                                    else:
                                        t.is_searchable = t.is_editable = False
                                        t.search_expression = None
                                    intermediate_nodes[original_name, child_key] = t
                            else:
                                t = tag
                                if not in_uc:
                                    t.original_name = t.name
                                intermediate_nodes[t.original_name, child_key] = t
                            t.is_hierarchical = \
                                '5state' if t.category != 'search' else '3state'
                            t.name = comp
                            node_parent = self.create_node(parent=node_parent,
                                               data=t, is_gst=is_gst, tooltip=tt,
                                               icon_map=self.icon_state_map)
                            child_map[(comp, child_key)] = node_parent

                        # Correct the average rating for the node
                        total = count = 0
                        for book_id in t.id_set:
                            rating = book_rating_map.get(book_id, 0)
                            if rating:
                                total += rating/2.0
                                count += 1
                        node_parent.cached_average_rating = float(total)/count if total and count else 0
            return
        # }}}

        # Build the entire node tree. Note that category_nodes is in field
        # metadata order so the User categories will be at the end
        with self.db.new_api.safe_read_lock:  # needed as we read from book_value_map
            for category in self.category_nodes:
                process_one_node(category, collapse_model, self.db.new_api.fields['rating'].book_value_map,
                                state_map.get(category.category_key, {}))

        # Fix up the node tree, reordering as needed and deleting undisplayed
        # nodes. First, remove empty user category subnodes if needed. This is a
        # recursive process because the hierarchical categories were combined
        # together in process_one_node (above), which also computes the child
        # count.
        if self.prefs['tag_browser_hide_empty_categories']:
            def process_uc_children(parent, depth):
                new_children = []
                for node in parent.children:
                    if node.type == TagTreeItem.CATEGORY:
                        # I could De Morgan's this but I think it is more
                        # understandable this way
                        if node.category_key.startswith('@') and len(node.children) == 0:
                            pass
                        else:
                            new_children.append(node)
                            process_uc_children(node, depth+1)
                    else:
                        new_children.append(node)
                parent.children = new_children
            for node in self.root_item.children:
                if node.category_key.startswith('@'):
                    process_uc_children(node, 1)

        # Now check the standard categories and root-level user categories,
        # removing any hidden categories and if needed, empty categories
        new_children = []
        for node in self.root_item.children:
            if self.prefs['tag_browser_hide_empty_categories'] and len(node.child_tags()) == 0:
                continue
            key = node.category_key
            if key in self.row_map:
                if self.hidden_categories:
                    if key in self.hidden_categories:
                        continue
                    found = False
                    for cat in self.hidden_categories:
                        if cat.startswith('@') and key.startswith(cat + '.'):
                            found = True
                    if found:
                        continue
                new_children.append(node)
        self.root_item.children = new_children
        self.root_item.children.sort(key=lambda x: self.row_map.index(x.category_key))
        if self.set_in_tag_browser():
            self.research_required.emit()

    def set_in_tag_browser(self):
        # If the filter isn't set then don't build the list, improving
        # performance significantly for large libraries or libraries with lots
        # of categories. This means that in_tag_browser:true with no filter will
        # return all books. This is incorrect in the rare case where the
        # category list in the tag browser doesn't contain a category like
        # authors that by definition matches all books because all books have an
        # author. If really needed the user can work around this 'error' by
        # clicking on the categories of interest with the connector set to 'or'.
        if self.filter_categories_by:
            id_set = set()
            for x in (a for a in self.root_item.children if a.category_key != 'search' and not a.is_gst):
                for t in x.child_tags():
                    id_set |= t.tag.id_set
        else:
            id_set = None
        changed = self.db.data.get_in_tag_browser() != id_set
        self.db.data.set_in_tag_browser(id_set)
        return changed

    def get_category_editor_data(self, category):
        for cat in self.root_item.children:
            if cat.category_key == category:
                return [(t.tag.id, t.tag.original_name, t.tag.count)
                        for t in cat.child_tags() if t.tag.count > 0]

    def is_in_user_category(self, index):
        if not index.isValid():
            return False
        p = self.get_node(index)
        while p.type != TagTreeItem.CATEGORY:
            p = p.parent
        return p.tag.category.startswith('@')

    def is_key_a_hierarchical_category(self, key):
        result = self.hierarchical_categories.get(key)
        if result is None:
            result = not (
                    key in ['authors', 'publisher', 'news', 'formats', 'rating'] or
                    key not in self.db.new_api.pref('categories_using_hierarchy', []) or
                    config['sort_tags_by'] != 'name')
            self.hierarchical_categories[key] = result
        return result

    def is_index_on_a_hierarchical_category(self, index):
        if not index.isValid():
            return False
        p = self.get_node(index)
        return self.is_key_a_hierarchical_category(p.tag.category)

    # Drag'n Drop {{{
    def mimeTypes(self):
        return ['application/calibre+from_library',
                'application/calibre+from_tag_browser']

    def mimeData(self, indexes):
        data = []
        for idx in indexes:
            if idx.isValid():
                # get some useful serializable data
                node = self.get_node(idx)
                path = self.path_for_index(idx)
                if node.type == TagTreeItem.CATEGORY:
                    d = (node.type, node.py_name, node.category_key)
                else:
                    t = node.tag
                    p = node
                    while p.type != TagTreeItem.CATEGORY:
                        p = p.parent
                    d = (node.type, p.category_key, p.is_gst, t.original_name,
                         t.category, path)
                data.append(d)
            else:
                data.append(None)
        raw = bytearray(json_dumps(data))
        ans = QMimeData()
        ans.setData('application/calibre+from_tag_browser', raw)
        return ans

    def dropMimeData(self, md, action, row, column, parent):
        fmts = {str(x) for x in md.formats()}
        if not fmts.intersection(set(self.mimeTypes())):
            return False
        if 'application/calibre+from_library' in fmts:
            if action != Qt.DropAction.CopyAction:
                return False
            return self.do_drop_from_library(md, action, row, column, parent)
        elif 'application/calibre+from_tag_browser' in fmts:
            return self.do_drop_from_tag_browser(md, action, row, column, parent)

    def do_drop_from_tag_browser(self, md, action, row, column, parent):
        if not parent.isValid():
            return False
        dest = self.get_node(parent)
        if not md.hasFormat('application/calibre+from_tag_browser'):
            return False
        data = bytes(md.data('application/calibre+from_tag_browser'))
        src = json_loads(data)
        if len(src) == 1:
            # Check to see if this is a hierarchical rename
            s = src[0]
            # This check works for both hierarchical and user categories.
            # We can drag only tag items.
            if s[0] != TagTreeItem.TAG:
                return False
            src_index = self.index_for_path(s[5])
            if src_index == parent:
                # dropped on itself
                return False
            src_item = self.get_node(src_index)
            dest_item = parent.data(Qt.ItemDataRole.UserRole)
            # Here we do the real work. If src is a tag, src == dest, and src
            # is hierarchical then we can do a rename.
            if (src_item.type == TagTreeItem.TAG and
                    src_item.tag.category == dest_item.tag.category and
                    self.is_key_a_hierarchical_category(src_item.tag.category)):
                key = s[1]
                # work out the part of the source name to use in the rename
                # It isn't necessarily a simple name but might be the remaining
                # levels of the hierarchy
                part = src_item.tag.original_name.rpartition('.')
                src_simple_name = part[2]
                # work out the new prefix, the destination node name
                if dest.type == TagTreeItem.TAG:
                    new_name = dest_item.tag.original_name + '.' + src_simple_name
                else:
                    new_name = src_simple_name
                if self.get_in_vl():
                    src_item.use_vl = rename_only_in_vl_question(self.gui_parent)
                else:
                    src_item.use_vl = False
                self.rename_item(src_item, key, new_name)
                return True
        # Should be working with a user category
        if dest.type != TagTreeItem.CATEGORY:
            return False
        return self.move_or_copy_item_to_user_category(src, dest, action)

    def move_or_copy_item_to_user_category(self, src, dest, action):
        '''
        src is a list of tuples representing items to copy. The tuple is
        (type, containing category key, category key is global search term,
         full name, category key, path to node)
        The type must be TagTreeItem.TAG
        dest is the TagTreeItem node to receive the items
        action is Qt.DropAction.CopyAction or Qt.DropAction.MoveAction
        '''
        def process_source_node(user_cats, src_parent, src_parent_is_gst,
                                is_uc, dest_key, idx):
            '''
            Copy/move an item and all its children to the destination
            '''
            copied = False
            src_name = idx.tag.original_name
            src_cat = idx.tag.category
            # delete the item if the source is a User category and action is move
            if is_uc and not src_parent_is_gst and src_parent in user_cats and \
                                    action == Qt.DropAction.MoveAction:
                new_cat = []
                for tup in user_cats[src_parent]:
                    if src_name == tup[0] and src_cat == tup[1]:
                        continue
                    new_cat.append(list(tup))
                user_cats[src_parent] = new_cat
            else:
                copied = True

            # Now add the item to the destination User category
            add_it = True
            if not is_uc and src_cat == 'news':
                src_cat = 'tags'
            for tup in user_cats[dest_key]:
                if src_name == tup[0] and src_cat == tup[1]:
                    add_it = False
            if add_it:
                user_cats[dest_key].append([src_name, src_cat, 0])

            for c in idx.children:
                copied = process_source_node(user_cats, src_parent, src_parent_is_gst,
                                             is_uc, dest_key, c)
            return copied

        user_cats = self.db.new_api.pref('user_categories', {})
        path = None
        for s in src:
            src_parent, src_parent_is_gst = s[1:3]
            path = s[5]

            if src_parent.startswith('@'):
                is_uc = True
                src_parent = src_parent[1:]
            else:
                is_uc = False
            dest_key = dest.category_key[1:]

            if dest_key not in user_cats:
                continue

            idx = self.index_for_path(path)
            if idx.isValid():
                process_source_node(user_cats, src_parent, src_parent_is_gst,
                                             is_uc, dest_key,
                                             self.get_node(idx))

        self.db.new_api.set_pref('user_categories', user_cats)
        self.refresh_required.emit()
        self.user_category_added.emit()
        return True

    def do_drop_from_library(self, md, action, row, column, parent):
        idx = parent
        if idx.isValid():
            node = self.data(idx, Qt.ItemDataRole.UserRole)
            if node.type == TagTreeItem.TAG:
                fm = self.db.metadata_for_field(node.tag.category)
                if node.tag.category in \
                    ('tags', 'series', 'authors', 'rating', 'publisher', 'languages', 'formats') or \
                    (fm['is_custom'] and (
                            fm['datatype'] in ['text', 'rating', 'series',
                                               'enumeration'] or (
                                                   fm['datatype'] == 'composite' and
                                                   fm['display'].get('make_category', False)))):
                    mime = 'application/calibre+from_library'
                    ids = list(map(int, md.data(mime).data().split()))
                    self.handle_drop(node, ids)
                    return True
            elif node.type == TagTreeItem.CATEGORY:
                fm_dest = self.db.metadata_for_field(node.category_key)
                if fm_dest['kind'] == 'user':
                    fm_src = self.db.metadata_for_field(md.column_name)
                    if md.column_name in ['authors', 'publisher', 'series'] or (
                               (fm_src['is_custom'] and (
                                fm_src['datatype'] in ['series', 'text', 'enumeration'] and
                                not fm_src['is_multiple'])) or
                               (fm_src['datatype'] == 'composite' and
                                fm_src['display'].get('make_category', False))
                            ):
                        mime = 'application/calibre+from_library'
                        ids = list(map(int, md.data(mime).data().split()))
                        self.handle_user_category_drop(node, ids, md.column_name)
                        return True
        return False

    def handle_user_category_drop(self, on_node, ids, column):
        categories = self.db.new_api.pref('user_categories', {})
        cat_contents = categories.get(on_node.category_key[1:], None)
        if cat_contents is None:
            return
        cat_contents = {(v, c) for v,c,ign in cat_contents}

        fm_src = self.db.metadata_for_field(column)
        label = fm_src['label']

        for id in ids:
            if not fm_src['is_custom']:
                if label == 'authors':
                    value = self.db.authors(id, index_is_id=True)
                    value = [v.replace('|', ',') for v in value.split(',')]
                elif label == 'publisher':
                    value = self.db.publisher(id, index_is_id=True)
                elif label == 'series':
                    value = self.db.series(id, index_is_id=True)
            else:
                if fm_src['datatype'] != 'composite':
                    value = self.db.get_custom(id, label=label, index_is_id=True)
                else:
                    value = self.db.get_property(id, loc=fm_src['rec_index'],
                                                 index_is_id=True)
            if value:
                if not isinstance(value, list):
                    value = [value]
                cat_contents |= {(v, column) for v in value}

        categories[on_node.category_key[1:]] = [[v, c, 0] for v,c in cat_contents]
        self.db.new_api.set_pref('user_categories', categories)
        self.refresh_required.emit()
        self.user_category_added.emit()

    def handle_drop_on_format(self, fmt, book_ids):
        self.convert_requested.emit(book_ids, fmt)

    def handle_drop(self, on_node, ids):
        # print('Dropped ids:', ids, on_node.tag)
        key = on_node.tag.category
        if key == 'formats':
            self.handle_drop_on_format(on_node.tag.name, ids)
            return
        if (key == 'authors' and len(ids) >= 5):
            if not confirm('<p>'+_('Changing the authors for several books can '
                           'take a while. Are you sure?') +
                           '</p>', 'tag_browser_drop_authors', self.gui_parent):
                return
        elif len(ids) > 15:
            if not confirm('<p>'+_('Changing the metadata for that many books '
                           'can take a while. Are you sure?') +
                           '</p>', 'tag_browser_many_changes', self.gui_parent):
                return

        fm = self.db.metadata_for_field(key)
        is_multiple = fm['is_multiple']
        val = on_node.tag.original_name
        for id in ids:
            mi = self.db.get_metadata(id, index_is_id=True)

            # Prepare to ignore the author, unless it is changed. Title is
            # always ignored -- see the call to set_metadata
            set_authors = False

            # Author_sort cannot change explicitly. Changing the author might
            # change it.
            mi.author_sort = None  # Never will change by itself.

            if key == 'authors':
                mi.authors = [val]
                set_authors=True
            elif fm['datatype'] == 'rating':
                mi.set(key, len(val) * 2)
            elif fm['datatype'] == 'series':
                series_index = self.db.new_api.get_next_series_num_for(val, field=key)
                if fm['is_custom']:
                    mi.set(key, val, extra=series_index)
                else:
                    mi.series, mi.series_index = val, series_index
            elif is_multiple:
                new_val = mi.get(key, [])
                if val in new_val:
                    # Fortunately, only one field can change, so the continue
                    # won't break anything
                    continue
                new_val.append(val)
                mi.set(key, new_val)
            else:
                mi.set(key, val)
            self.db.set_metadata(id, mi, set_title=False,
                                 set_authors=set_authors, commit=False)
        self.db.commit()
        self.drag_drop_finished.emit(ids)
    # }}}

    def get_in_vl(self):
        return self.db.data.get_base_restriction() or self.db.data.get_search_restriction()

    def get_book_ids_to_use(self):
        if self.db.data.get_base_restriction() or self.db.data.get_search_restriction():
            return self.db.search('', return_matches=True, sort_results=False)
        return None

    def get_ordered_categories(self, use_defaults=False, pref_data_override=None):
        if use_defaults:
            tbo = []
        elif pref_data_override:
            tbo = [k for k,_ in pref_data_override]
        else:
            tbo = self.db.new_api.pref('tag_browser_category_order', [])
        return category_display_order(tbo, list(self.categories.keys()))

    def _get_category_nodes(self, sort):
        '''
        Called by __init__. Do not directly call this method.
        '''
        self.row_map = []
        self.categories = OrderedDict()

        # We need to pass this to get_categories so it can adjust how it sorts
        # the values. The "first_letter_sort" argument is the default. It is
        # changed to False by get_categories() if the category is not collapsed
        uncollapsed_categories = self.db.prefs.get('tag_browser_dont_collapse',
                                                   self.prefs['tag_browser_dont_collapse'])

        # Get the categories
        try:
            # We must disable the in_tag_browser ids because we want all the
            # categories that will be filtered later. They might be restricted
            # by a VL or extra restriction.
            old_in_tb = self.db.data.get_in_tag_browser()
            self.db.data.set_in_tag_browser(None)
            data = self.db.new_api.get_categories(sort=sort,
                    book_ids=self.get_book_ids_to_use(),
                    first_letter_sort=(self.collapse_model == 'first letter'),
                    uncollapsed_categories=uncollapsed_categories)
            self.db.data.set_in_tag_browser(old_in_tb)
        except Exception as e:
            traceback.print_exc()
            data = self.db.new_api.get_categories(sort=sort,
                    first_letter_sort=(self.collapse_model == 'first letter'),
                    uncollapsed_categories=uncollapsed_categories)
            self.restriction_error.emit(str(e))

        if self.filter_categories_by:
            if self.filter_categories_by.startswith('='):
                use_exact_match = True
                filter_by = self.filter_categories_by[1:]
            else:
                use_exact_match = False
                filter_by = self.filter_categories_by

            if prefs['use_primary_find_in_search']:
                def final_equals(x, y):
                    return primary_strcmp(x, y) == 0
                def final_contains(x, y):
                    return primary_contains(x, y)
            else:
                def final_equals(x, y):
                    return strcmp(x, y) == 0
                def final_contains(filt, txt):
                    return contains(filt, icu_lower(txt))

            for category in data.keys():
                if use_exact_match:
                    data[category] = [t for t in data[category]
                        if final_equals(t.name, filter_by)]
                else:
                    data[category] = [t for t in data[category]
                        if final_contains(filter_by, t.name)]

        # Build a dict of the keys that have data.
        # Always add user categories so that the constructed hierarchy works.
        # This means that empty categories will be displayed unless the 'hide
        # empty categories' box is checked.
        tb_categories = self.db.field_metadata
        for category in tb_categories:
            if category in data or category.startswith('@'):
                self.categories[category] = tb_categories[category]['name']

        # Now build the list of fields in display order. A lot of this is to
        # maintain compatibility with the tweaks.
        order_pref = self.db.new_api.pref('tag_browser_category_order', None)
        if order_pref is not None:
            # Keys are in order
            self.row_map = self.get_ordered_categories()
        else:
            order = tweaks.get('tag_browser_category_default_sort', 'default')
            self.row_map = list(self.categories.keys())
            if order not in ('default', 'display_name', 'lookup_name'):
                print('Tweak tag_browser_category_default_sort is not valid. Ignored')
                order = 'default'
            if order != 'default':
                def key_func(val):
                    if order == 'display_name':
                        return icu_lower(self.db.field_metadata[val]['name'])
                    return icu_lower(val[1:] if val.startswith(('#', '@')) else val)
                direction = tweaks.get('tag_browser_category_default_sort_direction', 'ascending')
                if direction not in ('ascending', 'descending'):
                    print('Tweak tag_browser_category_default_sort_direction is not valid. Ignored')
                    direction = 'ascending'
                self.row_map.sort(key=key_func, reverse=direction == 'descending')
                try:
                    order = tweaks.get('tag_browser_category_order', {'*':1})
                    if not isinstance(order, dict):
                        raise TypeError()
                except Exception:
                    print('Tweak tag_browser_category_order is not valid. Ignored')
                    order = {'*': 1000}
                defvalue = order.get('*', 1000)
                self.row_map.sort(key=lambda x: order.get(x, defvalue))
            # Migrate the tweak to the new pref. First, make sure the order is valid
            self.row_map = self.get_ordered_categories(pref_data_override=[[k,None] for k in self.row_map])
            self.db.new_api.set_pref('tag_browser_category_order', self.row_map)
        return data

    def set_categories_filter(self, txt):
        if txt:
            self.filter_categories_by = icu_lower(txt)
        else:
            self.filter_categories_by = None

    def get_categories_filter(self):
        return self.filter_categories_by

    def refresh(self, data=None):
        '''
        Here to trap usages of refresh in the old architecture. Can eventually
        be removed.
        '''
        print('TagsModel: refresh called!')
        traceback.print_stack()
        return False

    def create_node(self, *args, **kwargs):
        node = TagTreeItem(*args, **kwargs)
        self.node_map[id(node)] = node
        node.category_custom_icons = self.category_custom_icons
        node.value_icons = self.value_icons
        node.value_icon_cache = self.value_icon_cache
        node.icon_config_dir = self.icon_config_dir
        return node

    def get_node(self, idx):
        ans = self.node_map.get(idx.internalId(), self.root_item)
        return ans

    def createIndex(self, row, column, internal_pointer=None):
        idx = QAbstractItemModel.createIndex(self, row, column,
                id(internal_pointer))
        return idx

    def category_row_map(self):
        return {category.category_key:row for row, category in enumerate(self.root_item.children)}

    def index_for_category(self, name):
        for row, category in enumerate(self.root_item.children):
            if category.category_key == name:
                return self.index(row, 0, QModelIndex())

    def columnCount(self, parent):
        return 1

    def data(self, index, role):
        if not index.isValid():
            return None
        item = self.get_node(index)
        return item.data(role)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid():
            return False
        # set up to reposition at the same item. We can do this except if
        # working with the last item and that item is deleted, in which case
        # we position at the parent label
        val = str(value or '').strip()
        if not val:
            return self.show_error_after_event_loop_tick(_('Item is blank'),
                        _('An item cannot be set to nothing. Delete it instead.'))
        item = self.get_node(index)
        if item.type == TagTreeItem.CATEGORY and item.category_key.startswith('@'):
            if val.find('.') >= 0:
                return self.show_error_after_event_loop_tick(_('Rename User category'),
                    _('You cannot use periods in the name when '
                      'renaming User categories'))

            user_cats = self.db.new_api.pref('user_categories', {})
            user_cat_keys_lower = [icu_lower(k) for k in user_cats]
            ckey = item.category_key[1:]
            ckey_lower = icu_lower(ckey)
            dotpos = ckey.rfind('.')
            if dotpos < 0:
                nkey = val
            else:
                nkey = ckey[:dotpos+1] + val
            nkey_lower = icu_lower(nkey)

            if ckey == nkey:
                self.use_position_based_index_on_next_recount = True
                return True

            for c in sorted(user_cats.keys(), key=sort_key):
                if icu_lower(c).startswith(ckey_lower):
                    if len(c) == len(ckey):
                        if strcmp(ckey, nkey) != 0 and \
                                nkey_lower in user_cat_keys_lower:
                            return self.show_error_after_event_loop_tick(_('Rename User category'),
                                _('The name %s is already used')%nkey)
                        user_cats[nkey] = user_cats[ckey]
                        self.rename_user_category_icon('@' + c, '@' + nkey)
                        del user_cats[ckey]
                    elif c[len(ckey)] == '.':
                        rest = c[len(ckey):]
                        if strcmp(ckey, nkey) != 0 and \
                                    icu_lower(nkey + rest) in user_cat_keys_lower:
                            return self.show_error_after_event_loop_tick(_('Rename User category'),
                                _('The name %s is already used')%(nkey + rest))
                        user_cats[nkey + rest] = user_cats[ckey + rest]
                        self.rename_user_category_icon('@' + ckey + rest, '@' + nkey + rest)
                        del user_cats[ckey + rest]
            self.user_categories_edited.emit(user_cats, nkey)  # Does a refresh
            self.use_position_based_index_on_next_recount = True
            return True

        key = item.tag.category
        # make certain we know about the item's category
        if key not in self.db.field_metadata:
            return False
        if key == 'authors':
            if val.find('&') >= 0:
                return self.show_error_after_event_loop_tick(_('Invalid author name'),
                        _('Author names cannot contain & characters.'))
                return False
        if key == 'search':
            if val == str(item.data(role) or ''):
                return True
            if val in self.db.saved_search_names():
                return self.show_error_after_event_loop_tick(
                    _('Duplicate search name'), _('The saved search name %s is already used.')%val)
            self.use_position_based_index_on_next_recount = True
            self.db.saved_search_rename(str(item.data(role) or ''), val)
            item.tag.name = val
            self.search_item_renamed.emit()  # Does a refresh
        else:
            self.rename_item(item, key, val)
        return True

    def show_error_after_event_loop_tick(self, title, msg, det_msg=''):
        self.show_error_after_event_loop_tick_signal.emit(title, msg, det_msg)
        return False

    def on_show_error_after_event_loop_tick(self, title, msg, details):
        error_dialog(self.gui_parent, title, msg, det_msg=details, show=True)

    def rename_item(self, item, key, to_what):
        def do_one_item(lookup_key, an_item, original_name, new_name, restrict_to_books):
            self.use_position_based_index_on_next_recount = True
            self.db.new_api.rename_items(lookup_key, {an_item.tag.id: new_name},
                                         restrict_to_book_ids=restrict_to_books)
            self.tag_item_renamed.emit()
            val_icon_data = self.value_icons.get(an_item.tag.category, {}).get(an_item.tag.original_name)
            if val_icon_data:
                # There is an icon for the old value. Rename it
                self.value_icons[an_item.tag.category].pop(an_item.tag.original_name, None)
                self.value_icons[an_item.tag.category][new_name] = val_icon_data
            an_item.tag.name = new_name
            an_item.tag.state = TAG_SEARCH_STATES['clear']
            self.use_position_based_index_on_next_recount = True
            self.add_renamed_item_to_user_categories(lookup_key, original_name, new_name)

        children = item.all_children()
        restrict_to_book_ids = self.get_book_ids_to_use() if item.use_vl else None
        if item.tag.is_editable and len(children) == 0:
            # Leaf node, just do it.
            do_one_item(key, item, item.tag.original_name, to_what, restrict_to_book_ids)
        else:
            # Middle node of a hierarchy
            search_name = item.tag.original_name
            # Clear any search icons on the original tag
            if item.parent.type == TagTreeItem.TAG:
                item.parent.tag.state = TAG_SEARCH_STATES['clear']
            # It might also be a leaf
            if item.tag.is_editable:
                do_one_item(key, item, item.tag.original_name, to_what, restrict_to_book_ids)
            # Now do the children
            for child_item in children:
                from calibre.utils.icu import startswith
                if (child_item.tag.is_editable and
                        startswith(child_item.tag.original_name, search_name)):
                    new_name = to_what + child_item.tag.original_name[len(search_name):]
                    do_one_item(key, child_item, child_item.tag.original_name,
                                new_name, restrict_to_book_ids)
        self.clean_items_from_user_categories()
        self.refresh_required.emit()

    def rename_item_in_all_user_categories(self, item_name, item_category, new_name):
        '''
        Search all User categories for items named item_name with category
        item_category and rename them to new_name. The caller must arrange to
        redisplay the tree as appropriate.
        '''
        user_cats = self.db.new_api.pref('user_categories', {})
        for k in user_cats.keys():
            ucat = {n:c for n,c,_ in user_cats[k]}
            # Check if the new name with the same category already exists. If
            # so, remove the old name because it would be a duplicate. This can
            # happen if two items in the item_category were renamed to the same
            # name.
            if ucat.get(new_name, None) == item_category:
                if ucat.pop(item_name, None) is not None:
                    # Only update the user_cats when something changes
                    user_cats[k] = [(n, c, 0) for n, c in ucat.items()]
            elif ucat.get(item_name, None) == item_category:
                # If the old name/item_category exists, rename it to the new
                # name using del/add
                del ucat[item_name]
                ucat[new_name] = item_category
                user_cats[k] = [(n, c, 0) for n, c in ucat.items()]
        self.db.new_api.set_pref('user_categories', user_cats)

    def delete_item_from_all_user_categories(self, item_name, item_category):
        '''
        Search all User categories for items named item_name with category
        item_category and delete them. The caller must arrange to redisplay the
        tree as appropriate.
        '''
        user_cats = self.db.new_api.pref('user_categories', {})
        for cat in user_cats.keys():
            self.delete_item_from_user_category(cat, item_name, item_category,
                                                user_categories=user_cats)
        self.db.new_api.set_pref('user_categories', user_cats)

    def delete_item_from_user_category(self, category, item_name, item_category,
                                       user_categories=None):
        if user_categories is not None:
            user_cats = user_categories
        else:
            user_cats = self.db.new_api.pref('user_categories', {})
        new_contents = []
        for tup in user_cats[category]:
            if tup[0] != item_name or tup[1] != item_category:
                new_contents.append(tup)
        user_cats[category] = new_contents
        if user_categories is None:
            self.db.new_api.set_pref('user_categories', user_cats)

    def add_renamed_item_to_user_categories(self, lookup_key, original_name, new_name):
        '''
        Add new_name to any user category that contains original name if new_name
        isn't already there. The original name isn't deleted. This is the first
        step when renaming user categories that might be in virtual libraries
        because when finished both names may still exist. You should call
        clean_items_from_user_categories() when done to remove any keys that no
        longer exist from all user categories. The caller must arrange to
        redisplay the tree as appropriate.
        '''
        user_cats = self.db.new_api.pref('user_categories', {})
        for cat in user_cats.keys():
            found_original = False
            found_new = False
            for name,key,_ in user_cats[cat]:
                if key == lookup_key:
                    if name == original_name:
                        found_original = True
                    if name == new_name:
                        found_new = True
            if found_original and not found_new:
                user_cats[cat].append([new_name, lookup_key, 0])
        self.db.new_api.set_pref('user_categories', user_cats)

    def clean_items_from_user_categories(self):
        '''
        Remove any items that no longer exist from user categories. This can
        happen when renaming items in virtual libraries, where sometimes the
        old name still exists on some book not in the VL and sometimes it
        doesn't. The caller must arrange to redisplay the tree as appropriate.
        '''
        user_cats = self.db.new_api.pref('user_categories', {})
        cache = self.db.new_api
        all_cats = {}
        for cat in user_cats.keys():
            new_cat = []
            for val, key, _ in user_cats[cat]:
                datatype = cache.field_metadata.get(key, {}).get('datatype')
                # datatype can be None if a column used in user categories has
                # been deleted. Remove it from the user categories
                if datatype is not None and datatype != 'composite':
                    id_ = cache.get_item_id(key, val, case_sensitive=True)
                    if id_ is not None:
                        v = cache.books_for_field(key, id_)
                        if v:
                            new_cat.append([val, key, 0])
            if new_cat:
                all_cats[cat] = new_cat
        self.db.new_api.set_pref('user_categories', all_cats)

    def headerData(self, *args):
        return None

    def flags(self, index, *args):
        ans = Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsEditable
        if index.isValid():
            node = self.data(index, Qt.ItemDataRole.UserRole)
            if node.type == TagTreeItem.TAG:
                tag = node.tag
                category = tag.category
                if (tag.is_editable or tag.is_hierarchical) and category != 'search':
                    ans |= Qt.ItemFlag.ItemIsDragEnabled
                fm = self.db.metadata_for_field(category)
                if category in \
                    ('tags', 'series', 'authors', 'rating', 'publisher', 'languages', 'formats') or \
                    (fm['is_custom'] and
                        fm['datatype'] in ['text', 'rating', 'series', 'enumeration']):
                    ans |= Qt.ItemFlag.ItemIsDropEnabled
            else:
                if node.type != TagTreeItem.CATEGORY or node.category_key != 'formats':
                    ans |= Qt.ItemFlag.ItemIsDropEnabled
        return ans

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction|Qt.DropAction.MoveAction

    def named_path_for_index(self, index):
        ans = []
        while index.isValid():
            node = self.get_node(index)
            if node is self.root_item:
                break
            ans.append(node.name_id)
            index = self.parent(index)
        return ans

    def index_for_named_path(self, named_path):
        parent = self.root_item
        ipath = []
        path = named_path[:]
        while path:
            q = path.pop()
            for i, c in enumerate(parent.children):
                if c.name_id == q:
                    ipath.append(i)
                    parent = c
                    break
            else:
                break
        return self.index_for_path(ipath)

    def path_for_index(self, index):
        ans = []
        while index.isValid():
            ans.append(index.row())
            index = self.parent(index)
        ans.reverse()
        return ans

    def index_for_path(self, path):
        parent = QModelIndex()
        for idx,v in enumerate(path):
            tparent = self.index(v, 0, parent)
            if not tparent.isValid():
                if v > 0 and idx == len(path) - 1:
                    # Probably the last item went away. Use the one before it
                    tparent = self.index(v-1, 0, parent)
                    if not tparent.isValid():
                        # Not valid. Use the last valid index
                        break
                else:
                    # There isn't one before it. Use the last valid index
                    break
            parent = tparent
        return parent

    def index(self, row, column, parent):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = self.get_node(parent)

        try:
            child_item = parent_item.children[row]
        except IndexError:
            return QModelIndex()

        ans = self.createIndex(row, column, child_item)
        return ans

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()

        child_item = self.get_node(index)
        parent_item = getattr(child_item, 'parent', None)

        if parent_item is self.root_item or parent_item is None:
            return QModelIndex()

        ans = self.createIndex(parent_item.row(), 0, parent_item)
        if not ans.isValid():
            return QModelIndex()
        return ans

    def rowCount(self, parent):
        if parent.column() > 0:
            return 0

        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = self.get_node(parent)

        return len(parent_item.children)

    def reset_all_states(self, except_=None):
        update_list = []

        def process_tag(tag_item):
            tag = tag_item.tag
            if tag is except_:
                tag_index = self.createIndex(tag_item.row(), 0, tag_item)
                self.dataChanged.emit(tag_index, tag_index)
            elif tag.state != 0 or tag in update_list:
                tag_index = self.createIndex(tag_item.row(), 0, tag_item)
                tag.state = 0
                update_list.append(tag)
                self.dataChanged.emit(tag_index, tag_index)
            for t in tag_item.children:
                process_tag(t)

        for t in self.root_item.children:
            process_tag(t)

    def clear_state(self):
        self.reset_all_states()

    def toggle(self, index, exclusive, set_to=None):
        '''
        exclusive: clear all states before applying this one
        set_to: None => advance the state, otherwise a value from TAG_SEARCH_STATES
        '''
        if not index.isValid():
            return False
        item = self.get_node(index)
        tag = item.tag
        if tag.category == 'search' and tag.search_expression is None:
            return False
        item.toggle(set_to=set_to)
        if exclusive:
            self.reset_all_states(except_=item.tag)
        self.dataChanged.emit(index, index)
        return True

    def tokens(self):
        ans = []
        # Tags can be in the news and the tags categories. However, because of
        # the desire to use two different icons (tags and news), the nodes are
        # not shared, which can lead to the possibility of searching twice for
        # the same tag. The tags_seen set helps us prevent that
        tags_seen = set()
        # Tag nodes are in their own category and possibly in User categories.
        # They will be 'checked' in both places, but we want to put the node
        # into the search string only once. The nodes_seen set helps us do that
        nodes_seen = set()
        stars = rating_to_stars(3, True)

        node_searches = {TAG_SEARCH_STATES['mark_plus']      : 'true',
                         TAG_SEARCH_STATES['mark_plusplus']  : '.true',
                         TAG_SEARCH_STATES['mark_minus']     : 'false',
                         TAG_SEARCH_STATES['mark_minusminus']: '.false'}

        for node in self.category_nodes:
            if node.tag.state:
                if node.category_key == 'news':
                    if node_searches[node.tag.state] == 'true':
                        ans.append('tags:"=' + _('News') + '"')
                    else:
                        ans.append('( not tags:"=' + _('News') + '")')
                else:
                    ans.append(f'{node.category_key}:{node_searches[node.tag.state]}')

            key = node.category_key
            for tag_item in node.all_children():
                if tag_item.type == TagTreeItem.CATEGORY:
                    if self.collapse_model == 'first letter' and \
                            tag_item.temporary and not key.startswith('@') \
                            and tag_item.tag.state:
                        k = 'author_sort' if key == 'authors' else key
                        letters_seen = {}
                        for subnode in tag_item.children:
                            if subnode.tag.sort:
                                c = subnode.tag.sort[0]
                                if c in r'\.^$[]|()':
                                    c = f'\\{c}'
                                letters_seen[c] = True
                        if letters_seen:
                            charclass = ''.join(letters_seen)
                            if k == 'author_sort':
                                expr = rf'{k}:"""~(^[{charclass}])|(&\s*[{charclass}])"""'
                            elif k == 'series':
                                expr = rf'series_sort:"""~^[{charclass}]"""'
                            else:
                                expr = rf'{k}:"""~^[{charclass}]"""'
                        else:
                            expr = rf'{k}:false'
                        if node_searches[tag_item.tag.state] == 'true':
                            ans.append(expr)
                        else:
                            ans.append('(not ' + expr + ')')
                    continue
                tag = tag_item.tag
                if tag.state != TAG_SEARCH_STATES['clear']:
                    if tag.state == TAG_SEARCH_STATES['mark_minus'] or \
                            tag.state == TAG_SEARCH_STATES['mark_minusminus']:
                        prefix = 'not '
                    else:
                        prefix = ''
                    if node.is_gst:
                        category = key
                    else:
                        category = tag.category if key != 'news' else 'tag'
                    add_colon = False
                    if self.db.field_metadata[tag.category]['is_csp']:
                        add_colon = True

                    if tag.name and tag.name[0] in stars:  # char is a star or a half. Assume rating
                        rnum = len(tag.name)
                        if tag.name.endswith(stars[-1]):
                            rnum = f'{rnum-1}.5'
                        ans.append(f'{prefix}{category}:{rnum}')
                    else:
                        name = tag.original_name
                        use_prefix = tag.state in [TAG_SEARCH_STATES['mark_plusplus'],
                                                   TAG_SEARCH_STATES['mark_minusminus']]
                        if category == 'tags':
                            if name in tags_seen:
                                continue
                            tags_seen.add(name)
                        if tag in nodes_seen:
                            continue
                        nodes_seen.add(tag)
                        n = name.replace(r'"', r'\"')
                        if name.startswith('.'):
                            n = '.' + n
                        ans.append('{}{}:"={}{}{}"'.format(prefix, category,
                                                '.' if use_prefix else '', n,
                                                ':' if add_colon else ''))
        return ans

    def find_item_node(self, key, txt, start_path, equals_match=False):
        '''
        Search for an item (a node) in the tags browser list that matches both
        the key (exact case-insensitive match) and txt (not equals_match =>
        case-insensitive contains match; equals_match => case_insensitive
        equal match). Returns the path to the node. Note that paths are to a
        location (second item, fourth item, 25 item), not to a node. If
        start_path is None, the search starts with the topmost node. If the tree
        is changed subsequent to calling this method, the path can easily refer
        to a different node or no node at all.
        '''
        if not txt:
            return None
        txt = lower(txt) if not equals_match else txt
        self.path_found = None
        if start_path is None:
            start_path = []

        if prefs['use_primary_find_in_search']:
            def final_equals(x, y):
                return primary_strcmp(x, y) == 0
            def final_contains(x, y):
                return primary_contains(x, y)
        else:
            def final_equals(x, y):
                return strcmp(x, y) == 0
            def final_contains(filt, txt):
                return contains(filt, icu_lower(txt))

        def process_tag(depth, tag_index, tag_item, start_path):
            path = self.path_for_index(tag_index)
            if depth < len(start_path) and path[depth] <= start_path[depth]:
                return False
            tag = tag_item.tag
            if tag is None:
                return False
            name = tag.original_name
            if ((equals_match and final_equals(name, txt)) or
                    (not equals_match and final_contains(txt, name))):
                self.path_found = path
                return True
            for i,c in enumerate(tag_item.children):
                if process_tag(depth+1, self.createIndex(i, 0, c), c, start_path):
                    return True
            return False

        def process_level(depth, category_index, start_path):
            path = self.path_for_index(category_index)
            if depth < len(start_path):
                if path[depth] < start_path[depth]:
                    return False
                if path[depth] > start_path[depth]:
                    start_path = path
            my_key = self.get_node(category_index).category_key
            for j in range(self.rowCount(category_index)):
                tag_index = self.index(j, 0, category_index)
                tag_item = self.get_node(tag_index)
                if tag_item.type == TagTreeItem.CATEGORY:
                    if process_level(depth+1, tag_index, start_path):
                        return True
                elif not key or strcmp(key, my_key) == 0:
                    if process_tag(depth+1, tag_index, tag_item, start_path):
                        return True
            return False

        for i in range(self.rowCount(QModelIndex())):
            if process_level(0, self.index(i, 0, QModelIndex()), start_path):
                break
        return self.path_found

    def find_category_node(self, key, parent=QModelIndex()):
        '''
        Search for an category node (a top-level node) in the tags browser list
        that matches the key (exact case-insensitive match). Returns the path to
        the node. Paths are as in find_item_node.
        '''
        if not key:
            return None

        for i in range(self.rowCount(parent)):
            idx = self.index(i, 0, parent)
            node = self.get_node(idx)
            if node.type == TagTreeItem.CATEGORY:
                ckey = node.category_key
                if strcmp(ckey, key) == 0:
                    return self.path_for_index(idx)
                if len(node.children):
                    v = self.find_category_node(key, idx)
                    if v is not None:
                        return v
        return None

    def set_boxed(self, idx):
        tag_item = self.get_node(idx)
        tag_item.boxed = True
        self.dataChanged.emit(idx, idx)

    def clear_boxed(self):
        '''
        Clear all boxes around items.
        '''
        def process_tag(tag_index, tag_item):
            if tag_item.boxed:
                tag_item.boxed = False
                self.dataChanged.emit(tag_index, tag_index)
            for i,c in enumerate(tag_item.children):
                process_tag(self.index(i, 0, tag_index), c)

        def process_level(category_index):
            for j in range(self.rowCount(category_index)):
                tag_index = self.index(j, 0, category_index)
                tag_item = self.get_node(tag_index)
                if tag_item.boxed:
                    tag_item.boxed = False
                    self.dataChanged.emit(tag_index, tag_index)
                if tag_item.type == TagTreeItem.CATEGORY:
                    process_level(tag_index)
                else:
                    process_tag(tag_index, tag_item)

        for i in range(self.rowCount(QModelIndex())):
            process_level(self.index(i, 0, QModelIndex()))

    # }}}
