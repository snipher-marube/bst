from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Get an item from a dictionary or DataFrame record"""
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