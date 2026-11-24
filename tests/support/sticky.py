def sticky_next[T](items: list[T]) -> T:
    return items.pop(0) if len(items) > 1 else items[0]
