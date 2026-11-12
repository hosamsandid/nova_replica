# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections.abc
import functools
import inspect

import nova.context
from nova import exception
from nova.i18n import _


def require_context(f):
    """Decorator to require *any* user or admin context.

    This does no authorization for user or project access matching, see
    :py:func:`nova.context.authorize_project_context` and
    :py:func:`nova.context.authorize_user_context`.

    The first argument to the wrapped function must be the context.

    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        nova.context.require_context(args[0])
        return f(*args, **kwargs)
    wrapper.__signature__ = inspect.signature(f)
    return wrapper


def process_sort_params(
    sort_keys,
    sort_dirs,
    default_keys=['created_at', 'id'],
    default_dir='asc',
):
    """Process the sort parameters to include default keys.

    Creates a list of sort keys and a list of sort directions. Adds the default
    keys to the end of the list if they are not already included.

    When adding the default keys to the sort keys list, the associated
    direction is:

    1. The first element in the 'sort_dirs' list (if specified), else
    2. 'default_dir' value (Note that 'asc' is the default value since this is
       the default in sqlalchemy.utils.paginate_query)

    :param sort_keys: List of sort keys to include in the processed list
    :param sort_dirs: List of sort directions to include in the processed list
    :param default_keys: List of sort keys that need to be included in the
        processed list, they are added at the end of the list if not already
        specified.
    :param default_dir: Sort direction associated with each of the default
        keys that are not supplied, used when they are added to the processed
        list
    :returns: list of sort keys, list of sort directions
    :raise exception.InvalidInput: If more sort directions than sort keys
        are specified or if an invalid sort direction is specified
    """
    # Determine direction to use for when adding default keys
    if sort_dirs and len(sort_dirs) != 0:
        default_dir_value = sort_dirs[0]
    else:
        default_dir_value = default_dir

    # Create list of keys (do not modify the input list)
    if sort_keys:
        result_keys = list(sort_keys)
    else:
        result_keys = []

    # If a list of directions is not provided, use the default sort direction
    # for all provided keys
    if sort_dirs:
        result_dirs = []
        # Verify sort direction
        for sort_dir in sort_dirs:
            if sort_dir not in ('asc', 'desc'):
                msg = _("Unknown sort direction, must be 'desc' or 'asc'")
                raise exception.InvalidInput(reason=msg)
            result_dirs.append(sort_dir)
    else:
        result_dirs = [default_dir_value for _sort_key in result_keys]

    # Ensure that the key and direction length match
    while len(result_dirs) < len(result_keys):
        result_dirs.append(default_dir_value)
    # Unless more direction are specified, which is an error
    if len(result_dirs) > len(result_keys):
        msg = _("Sort direction size exceeds sort key size")
        raise exception.InvalidInput(reason=msg)

    # Ensure defaults are included
    for key in default_keys:
        if key not in result_keys:
            result_keys.append(key)
            result_dirs.append(default_dir_value)

    return result_keys, result_dirs


def _db_connection_type(db_connection: str) -> str:
    """Returns a lowercase symbol for the db type.

    This is useful when we need to change what we are doing per DB (like
    handling regexes). In a CellsV2 world it probably needs to do something
    better than use the database configuration string.
    """
    db_string = db_connection.split(':')[0].split('+')[0]
    return db_string.lower()


def _safe_regex_mysql(raw_string: str) -> str:
    """Make regex safe to MySQL.

    Certain items like ``|`` are interpreted raw by mysql REGEX. If you search
    for a single | then you trigger an error because it's expecting content on
    either side.

    For consistency sake we escape all ``|``. This does mean we wouldn't
    support something like foo|bar to match completely different things,
    however, one can argue putting such complicated regex into name search
    probably means you are doing this wrong.
    """
    return raw_string.replace('|', '\\|')


def get_regexp_ops(
    connection: str
) -> tuple[collections.abc.Callable[[str], str], str]:
    """Return safety filter and db opts for regex."""
    regex_safe_filters = {
        'mysql': _safe_regex_mysql
    }
    regexp_op_map = {
        'postgresql': '~',
        'mysql': 'REGEXP',
        'sqlite': 'REGEXP'
    }
    db_type = _db_connection_type(connection)

    return (
        regex_safe_filters.get(db_type, lambda x: x),
        regexp_op_map.get(db_type, 'LIKE'),
    )
