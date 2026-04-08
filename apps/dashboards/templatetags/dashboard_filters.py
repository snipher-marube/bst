"""
apps/dashboards/templatetags/dashboard_filters.py
==================================================
Custom Django template filters for the dashboard app.

Usage in templates::

    {% load dashboard_filters %}

    {# Look up a dynamic key in a record's data dict #}
    {{ record.data|get_item:column_name }}

    {# Safe fallback when the column might not exist #}
    {{ row|get_item:"amount"|default:"—" }}
"""
from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Look up *key* in *dictionary*, falling back gracefully to ``'-'``.

    Handles three cases:

    1. **dict-like**: tries ``dictionary[key]`` via the ``in`` operator.
    2. **object attribute**: falls back to ``getattr(dictionary, key, '-')``
       if the key subscription raises ``TypeError`` or ``KeyError``.
    3. **None or missing**: returns ``'-'`` so templates never render
       ``None``, ``"None"``, or an unhandled exception.

    Parameters
    ----------
    dictionary : dict | object | None
        The mapping or model instance to look up against.
    key : str
        The key or attribute name to retrieve.

    Returns
    -------
    Any
        The found value, or ``'-'`` when nothing matches.

    Examples
    --------
    In a template that renders a table whose columns are dynamic::

        {% for column in table.schema %}
          <td>{{ record.data|get_item:column.name }}</td>
        {% endfor %}
    """
    if dictionary is None:
        return '-'

    # Handle both dictionary and objects if needed
    try:
        if key in dictionary:
            return dictionary[key]
    except (TypeError, KeyError):
        try:
            return getattr(dictionary, key, '-')
        except AttributeError:
            pass

    return '-'