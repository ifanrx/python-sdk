# coding: utf-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import copy
import json
import warnings

import iso8601
from werkzeug.local import LocalProxy

import leancloud
from leancloud import utils
from leancloud import client
from leancloud import operation
from leancloud._compat import with_metaclass
from leancloud._compat import PY2
from leancloud._compat import text_type
from leancloud._compat import string_types
from leancloud._compat import iteritems


__author__ = 'asaka <lan@leancloud.rocks>'


object_class_map = {}


class ObjectMeta(type):
    def __new__(cls, name, bases, attrs):
        cached_class = object_class_map.get(name)
        if cached_class:
            return cached_class

        super_new = super(ObjectMeta, cls).__new__

        # let user define their class_name at subclass-creation stage
        class_name = attrs.pop('class_name', None)

        if class_name:
            attrs['_class_name'] = class_name
        elif name == 'User':
            attrs['_class_name'] = '_User'
        elif name == 'Installation':
            attrs['_class_name'] = '_Installation'
        elif name == 'Notification':
            attrs['_class_name'] = '_Notification'
        elif name == 'Role':
            attrs['_class_name'] = '_Role'
        else:
            attrs['_class_name'] = name

        object_class = super_new(cls, name, bases, attrs)
        object_class_map[name] = object_class
        return object_class

    @property
    def query(cls):
        """
        获取当前对象的 Query 对象。

        :rtype: leancloud.Query
        """
        return leancloud.Query(cls)


class Object(with_metaclass(ObjectMeta, object)):
    def __init__(self, **attrs):
        """
        创建一个新的 leancloud.Object

        :param attrs: 对象属性
        :return:
        """
        self.id = None
        self._class_name = self._class_name  # for IDE
        self._changes = {}
        self._attributes = {}
        self.created_at = None
        self.updated_at = None
        self.fetch_when_save = False

        for k, v in iteritems(attrs):
            self.set(k, v)

    @classmethod
    def extend(cls, name):
        """
        派生一个新的 leancloud.Object 子类

        :param name: 子类名称
        :type name: string_types
        :return: 派生的子类
        :rtype: ObjectMeta
        """
        if PY2 and isinstance(name, text_type):
            name = name.encode('utf-8')
        return type(name, (cls,), {})

    @classmethod
    def create(cls, class_name, **attributes):
        """
        根据参数创建一个 leancloud.Object 的子类的实例化对象

        :param class_name: 子类名称
        :type class_name: string_types
        :param attributes: 对象属性
        :return: 派生子类的实例
        :rtype: Object
        """
        object_class = cls.extend(class_name)
        return object_class(**attributes)

    @classmethod
    def create_without_data(cls, id_):
        """
        根据 objectId 创建一个 leancloud.Object，代表一个服务器上已经存在的对象。可以调用 fetch 方法来获取服务器上的数据

        :param id_: 对象的 objectId
        :type id_: string_types
        :return: 没有数据的对象
        :rtype: Object
        """
        if cls is Object:
            raise RuntimeError('can not call create_without_data on leancloud.Object')
        obj = cls()
        obj.id = id_
        return obj

    @classmethod
    def save_all(cls, objs):
        """
        在一个请求中 save 多个 leancloud.Object 对象实例。

        :param objs: 需要 save 的对象
        :type objs: list
        """
        if not objs:
            return
        return cls()._deep_save(objs, [])

    @classmethod
    def destroy_all(cls, objs):
        """
        在一个请求中 destroy 多个 leancloud.Object 对象实例。

        :param objs: 需要 destroy 的对象
        :type objs: list
        """
        if not objs:
            return
        if not all(x._class_name == objs[0]._class_name for x in objs):
            raise ValueError("destroy_all requires the argument object list's _class_names must be the same")
        if any(x.is_new() for x in objs):
            raise ValueError("Could not destroy unsaved object")
        ids = {x.id for x in objs}
        ids = ','.join(ids)
        client.delete('/classes/{0}/{1}'.format(objs[0]._class_name, ids))

    @property
    def attributes(self):
        warnings.warn('leancloud.Object.attributes should not be used any more, please use get or set instead', leancloud.errors.LeanCloudWarning)
        return self._attributes

    def dump(self):
        obj = self._dump()
        obj.pop('__type')
        obj.pop('className')
        return obj

    def _dump(self):
        obj = copy.deepcopy(self._attributes)
        for k, v in iteritems(obj):
            obj[k] = utils.encode(v)

        if self.id is not None:
            obj['objectId'] = self.id

        obj['__type'] = 'Object'
        obj['className'] = self._class_name
        return obj

    def destroy(self):
        """
        从服务器上删除这个对象

        :rtype: None
        """
        if not self.id:
            return
        client.delete('/classes/{0}/{1}'.format(self._class_name, self.id))

    def save(self, where=None):
        """
        将对象数据保存至服务器

        :return: None
        :rtype: None
        """
        if where and not isinstance(where, leancloud.Query):
            raise TypeError('where param type should be leancloud.Query, got %s', type(where))

        if where and where._query_class._class_name != self._class_name:
            raise TypeError('where param\'s class name not equal to the current object\'s class name')

        if where and self.is_new():
            raise TypeError('where params works only when leancloud.Object is saved')

        unsaved_children = []
        unsaved_files = []
        self._find_unsaved_children(self._attributes, unsaved_children, unsaved_files)
        if unsaved_children or unsaved_files:
            self._deep_save(unsaved_children, unsaved_files, exclude=self._attributes)

        data = self._dump_save()
        fetch_when_save = 'true' if self.fetch_when_save else 'false'

        if self.is_new():
            response = client.post('/classes/{0}?fetchWhenSave={1}'.format(self._class_name, fetch_when_save), data)
        else:
            url = '/classes/{0}/{1}?fetchWhenSave={2}'.format(self._class_name, self.id, fetch_when_save)
            if where:
                url += '&where=' + json.dumps(where.dump()['where'], separators=(',', ':'))
            response = client.put(url, data)

        self._update_data(response.json())

    def _deep_save(self, unsaved_children, unsaved_files, exclude=None):
        if exclude:
            unsaved_children = [x for x in unsaved_children if x != exclude]

        for f in unsaved_files:
            f.save()

        if not unsaved_children:
            return
        dumped_objs = []
        for obj in unsaved_children:
            if obj.id is None:
                method = 'POST'
                path = '/{0}/classes/{1}'.format(client.SERVER_VERSION, obj._class_name)
            else:
                method = 'PUT'
                path = '/{0}/classes/{1}/{2}'.format(client.SERVER_VERSION, obj._class_name, obj.id)
            body = obj._dump_save()
            dumped_obj = {
                'method': method,
                'path': path,
                'body': body,
            }
            dumped_objs.append(dumped_obj)

        response = client.post('/batch', params={'requests': dumped_objs}).json()

        errors = []
        for idx, obj in enumerate(unsaved_children):
            content = response[idx]
            error = content.get('error')
            if error:
                errors.append(leancloud.LeanCloudError(error.get('code'), error.get('error')))
            else:
                obj._update_data(content['success'])

        if errors:
            # TODO: how to raise list of errors?
            # raise MultipleValidationErrors(errors)
            # add test
            raise errors[0]

    @classmethod
    def _find_unsaved_children(cls, obj, children, files):

        def callback(o):
            if isinstance(o, Object):
                if o.is_dirty():
                    children.append(o)
                return

            if isinstance(o, leancloud.File):
                if not o.url or not o.id:
                    files.append(o)
                return

        utils.traverse_object(obj, callback)

    def is_dirty(self, attr=None):
        #consider renaming to is_changed?
        if attr:
            return attr in self._changes
        else:
            return bool(not self.id or self._changes)

    def _to_pointer(self):
        return {
            '__type': 'Pointer',
            'className': self._class_name,
            'objectId': self.id,
        }

    def _merge_metadata(self, server_data):
        for key in ('objectId', 'createdAt', 'updatedAt'):
            if server_data.get(key) is None:
                continue
            if key == 'objectId':
                self.id = server_data[key]
            else:
                if isinstance(server_data[key], string_types):
                    dt = iso8601.parse_date(server_data[key])
                elif server_data[key]['__type'] == 'Date':
                    dt = iso8601.parse_date(server_data[key]['iso'])
                else:
                    raise TypeError('Invalid date type')
                server_data[key] = dt
                if key == 'createdAt':
                    self.created_at = dt
                elif key == 'updatedAt':
                    self.updated_at = dt
                else:
                    raise TypeError

    def validate(self, attrs):
        if 'ACL' in attrs and not isinstance(attrs['ACL'], leancloud.ACL):
            raise TypeError('acl must be a ACL')
        return True

    def get(self, attr, deafult=None):
        """
        获取对象字段的值

        :param attr: 字段名
        :type attr: string_types
        :return: 字段值
        """
        return self._attributes.get(attr, deafult)

    def relation(self, attr):
        """
        返回对象上相应字段的 Relation

        :param attr: 字段名
        :type attr: string_types
        :return: Relation
        :rtype: leancloud.Relation
        """
        value = self.get(attr)
        if value is not None:
            if not isinstance(value, leancloud.Relation):
                raise TypeError('field %s is not Relation'.format(attr))
            value._ensure_parent_and_key(self, attr)
            return value
        return leancloud.Relation(self, attr)

    def has(self, attr):
        """
        判断此字段是否有值

        :param attr: 字段名
        :return: 当有值时返回 True， 否则返回 False
        :rtype: bool
        """
        return attr in self._attributes

    def set(self, key_or_attrs, value=None, unset=False):
        """
        在当前对象此字段上赋值

        :param key_or_attrs: 字段名，或者一个包含 字段名 / 值的 dict
        :type key_or_attrs: string_types or dict
        :param value: 字段值
        :param unset:
        :return: 当前对象，供链式调用
        """
        if isinstance(key_or_attrs, dict) and value is None:
            attrs = key_or_attrs
            keys = attrs.keys()
            for k in keys:
                if isinstance(attrs[k], LocalProxy):
                    attrs[k] = attrs[k]._get_current_object()
        else:
            key = key_or_attrs
            if isinstance(value, LocalProxy):
                value = value._get_current_object()
            attrs = {key: utils.decode(key, value)}

        if unset:
            for k in attrs.keys():
                attrs[k] = operation.Unset()

        self.validate(attrs)

        self._merge_metadata(attrs)

        keys = list(attrs.keys())
        for k in keys:
            v = attrs[k]
            # TODO: Relation

            if not isinstance(v, operation.BaseOp):
                v = operation.Set(v)

            self._attributes[k] = v._apply(self._attributes.get(k),self, k)
            if self._attributes[k] == operation._UNSET:
                del self._attributes[k]
            self._changes[k] = v._merge(self._changes.get(k))

        return self

    def unset(self, attr):
        """
        在对象上移除此字段。

        :param attr: 字段名
        :return: 当前对象
        """
        return self.set(attr, None, unset=True)

    def increment(self, attr, amount=1):
        """
        在对象此字段上自增对应的数值，如果数值没有指定，默认为一。

        :param attr: 字段名
        :param amount: 自增量
        :return: 当前对象
        """
        return self.set(attr, operation.Increment(amount))

    def add(self, attr, item):
        """
        在对象此字段对应的数组末尾添加指定对象。

        :param attr: 字段名
        :param item: 要添加的对象
        :return: 当前对象
        """
        return self.set(attr, operation.Add([item]))

    def add_unique(self, attr, item):
        """
        在对象此字段对应的数组末尾添加指定对象，如果此对象并没有包含在字段中。

        :param attr: 字段名
        :param item: 要添加的对象
        :return: 当前对象
        """
        return self.set(attr, operation.AddUnique([item]))

    def remove(self, attr, item):
        """
        在对象此字段对应的数组中，将指定对象全部移除。

        :param attr: 字段名
        :param item: 要移除的对象
        :return: 当前对象
        """
        return self.set(attr, operation.Remove([item]))

    def clear(self):
        """
        将当前对象所有字段全部移除。

        :return: 当前对象
        """
        self.set(self._attributes, unset=True)

    def _dump_save(self):
        return {k:v.dump() for k,v in iteritems(self._changes)}

    def fetch(self):
        """
        从服务器获取当前对象所有的值，如果与本地值不同，将会覆盖本地的值。

        :return: 当前对象
        """
        response = client.get('/classes/{0}/{1}'.format(self._class_name, self.id), {})
        self._update_data(response.json())

    def is_new(self):
        """
        判断当前对象是否已经保存至服务器。

        :rtype: bool
        """
        return False if self.id else True

    def is_existed(self):
        return bool(self.id)

    def get_acl(self):
        """
        返回当前对象的 ACL。

        :return: 当前对象的 ACL
        :rtype: leancloud.ACL
        """
        return self.get('ACL')

    def set_acl(self, acl):
        """
        为当前对象设置 ACL

        :type acl: leancloud.ACL
        :return: 当前对象
        """

        return self.set('ACL', acl)

    def disable_before_hook(self):
        master_key = client.get_app_info().get('master_key')
        if not master_key:
            raise ValueError('disable_before_hook need LeanCloud master key')
        timestamp = int(time.time() * 1000)
        return self.set('__before', utils.sign_hook('__before_for_' + self._class_name, master_key, timestamp))

    def disable_after_hook(self):
        master_key = client.get_app_info().get('master_key')
        if not master_key:
            raise ValueError('disable_before_hook need LeanCloud master key')
        timestamp = int(time.time() * 1000)
        return self.set('__after', utils.sign_hook('__after_for_' + self._class_name, master_key, timestamp))

    def _update_data(self, server_data):
        self._merge_metadata(server_data)
        for key, value in iteritems(server_data):
            self._attributes[key] = utils.decode(key, value)
        self._changes = {}

    @staticmethod
    def as_class(arg):
        def inner_decorator(cls):
            cls._class_name = arg
            return cls
        return inner_decorator
