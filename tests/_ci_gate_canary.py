"""Deliberate CI-gate canary. Exists only on the throwaway proof branch."""


def wrong_return_type() -> int:
    """Declare int, return str."""
    return "definitely not an int"
