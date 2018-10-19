from copy import copy
try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

import six
import sqlalchemy as sa
from sqlalchemy_utils import identity, get_primary_keys, has_changes

class Operation(object):
    INSERT = 0
    UPDATE = 1
    DELETE = 2
    STALE_VERSION = -1

    def __init__(self, target, type):
        self.target = target
        self.type = type
        self.processed = False

    def __eq__(self, other):
        return (
            self.target == other.target and
            self.type == other.type
        )

    def __ne__(self, other):
        return not (self == other)


class Operations(object):
    """
    A collection of operations
    """
    def __init__(self):
        self.objects = OrderedDict()

    def format_key(self, target):
        # We cannot use target._sa_instance_state.identity here since object's
        # identity is not yet updated at this phase
        return (target.__class__, identity(target))

    def __contains__(self, target):
        return self.format_key(target) in self.objects

    def __setitem__(self, key, operation):
        self.objects[key] = operation

    def __getitem__(self, key):
        return self.objects[key]

    def __delitem__(self, key):
        del self.objects[key]

    def __bool__(self):
        return bool(self.objects)

    def __nonzero__(self):
        return self.__bool__()

    def __repr__(self):
        return repr(self.objects)

    @property
    def entities(self):
        """
        Return a set of changed versioned entities for given session.

        :param session: SQLAlchemy session object
        """
        return set(key[0] for key, _ in self.iteritems())

    def iteritems(self):
        return six.iteritems(self.objects)

    def items(self):
        return self.objects.items()

    def add(self, operation):
        self[self.format_key(operation.target)] = operation

    def add_insert(self, target):
        if target in self:
            # If the object is deleted and then inserted within the same
            # transaction we are actually dealing with an update.
            self.add(Operation(target, Operation.UPDATE))
        else:
            self.add(Operation(target, Operation.INSERT))

    def add_update(self, target):
        state_copy = copy(sa.inspect(target).committed_state)
        relationships = sa.inspect(target.__class__).relationships
        # Remove all ONETOMANY and MANYTOMANY relationships
        for rel_key, relationship in relationships.items():
            if relationship.direction.name in ['ONETOMANY', 'MANYTOMANY']:
                if rel_key in state_copy:
                    del state_copy[rel_key]

        if state_copy:
            self._sanitize_keys(target)
            key = self.format_key(target)
            # if the object has already been added with an INSERT,
            # then this is a modification within the same transaction and
            # this is still an INSERT
            if (target in self and
                self[key].type == Operation.INSERT):
                operation = Operation.INSERT
            else:
                operation = Operation.UPDATE

            self.add(Operation(target, operation))

    def add_delete(self, target):
        if target in self and \
           self[self.format_key(target)].type == Operation.INSERT:
            # if the target's existing operation is INSERT, it is being
            # deleted within the same transaction and no version entry
            # should be persisted
            self.add(Operation(target, Operation.STALE_VERSION))
        else:
            self.add(Operation(target, Operation.DELETE))

    def _sanitize_keys(self, target):
        """The operations key for target may not be valid if this target is in
        `self.objects` but its primary key has been modified. Check against that
        and update the key.
        """
        key = self.format_key(target)
        mapper = sa.inspect(target).mapper
        for pk in mapper.primary_key:
            if has_changes(target, mapper.get_property_by_column(pk).key):
                old_key = target.__class__, sa.inspect(target).identity
                if old_key in self.objects:
                    # replace old key with the new one
                    self.objects[key] = self.objects.pop(old_key)
                break