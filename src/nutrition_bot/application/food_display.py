"""Conservative labels derived only from immutable, reviewed food names."""

import re


def food_display_name(name: str, preparation: str) -> str:
    """Remove redundant punctuation/qualifiers without introducing a food alias.

    Comma-separated source names are usually catalog descriptions. Keep their
    ordering and every distinct qualifier, including cuts, fat and cooking method.
    Only whitespace and exact repeated comma components are removed. Unusual or
    long names remain intact rather than dropping potentially meaningful words.
    """
    components = []
    seen = set()
    for component in name.split(","):
        component = " ".join(component.split())
        if component and component.casefold() not in seen:
            components.append(component)
            seen.add(component.casefold())
    label = " ".join(components)
    preparation_label = preparation.replace("_", " ")
    if not re.search(r"\b" + re.escape(preparation_label) + r"\b", label, re.IGNORECASE):
        label += f" ({preparation_label})"
    return label
